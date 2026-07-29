"""Фото блюд — общий кэш по нормализованному slug.

Ключевой принцип стоимости: одно фото на канон-блюдо, генерится ОДИН раз
(Nano Banana / OpenRouter) и переиспользуется всеми юзерами навсегда. Значит
затраты = O(число разных блюд), а не O(юзеры × приёмы). Нормализация имени
(slug) схлопывает порции/падежи, чтобы «Овсянка 60 г» и «Овсянка» → один ключ.

Генерация ленивая, в фоне; пока фото нет — отдаётся SVG-плейсхолдер (см. app.py).
Единый стиль промпта → консистентная лента (не «AI-демка»).
"""
from __future__ import annotations

import base64
import io
import os
import re
import threading
from pathlib import Path

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGE_MODEL = os.getenv("NB_IMAGE_MODEL", "google/gemini-3.1-flash-image")  # Nano Banana 2

DATA = Path(os.getenv("DATA_DIR", "data"))
PHOTOS = DATA / "dishphotos"

# Единый фуд-фото стиль — консистентность важнее «вау» на отдельном кадре.
_STYLE = ("Appetizing realistic top-down food photograph of {title}. Served on a simple white "
          "ceramic plate on a light wooden table, soft natural daylight, fresh healthy home cooking, "
          "shallow depth of field, high detail, natural colors. Square composition. "
          "No text, no logos, no watermark, no hands, no people, no cutlery in frame.")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
# Предлоги/союзы/наполнители — не влияют на суть блюда, выкидываем из ключа.
_STOP = {"с", "и", "в", "во", "на", "по", "из", "от", "до", "для", "под", "без", "же",
         "а", "или", "к", "о", "об", "при", "со", "та", "тот", "это", "домашн", "свеж"}
_UNIT = r"(?:г|гр|кг|мл|л|шт|ст|стак\w*|стакан\w*|ложк\w*|порц\w*|ккал|kcal)"


def slugify(name: str) -> str:
    """Русское название блюда → стабильный ASCII-slug (общий ключ фото)."""
    s = (name or "").lower().strip()
    s = re.sub(r"\d+[.,]?\d*\s*" + _UNIT + r"\b", " ", s)   # порции «60 г», «200 мл»
    s = re.sub(r"\d+", " ", s)                                # оставшиеся цифры
    s = re.sub(r"[^\w\s-]", " ", s, flags=re.UNICODE)        # пунктуация
    words = [w for w in s.split() if w and w not in _STOP]
    tr = "".join(_TRANSLIT.get(ch, ch) for ch in " ".join(words))
    tr = re.sub(r"[^a-z0-9]+", "-", tr).strip("-")
    return tr[:60] or "dish"


def has_photo(slug: str) -> bool:
    return (PHOTOS / f"{slug}.webp").exists()


def photo_file(slug: str) -> Path:
    return PHOTOS / f"{slug}.webp"


def _key() -> str:
    if os.getenv("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for up in [Path(__file__).resolve().parents[i] for i in range(2, 8)]:
        env = up / ".hq" / "pipelines" / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(slug: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(slug)
        if lk is None:
            lk = _locks[slug] = threading.Lock()
        return lk


def _save_webp(slug: str, raw: bytes) -> None:
    from PIL import Image  # ленивый импорт — не роняет модуль, если Pillow нет
    PHOTOS.mkdir(parents=True, exist_ok=True)
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    s = min(w, h)
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    im = im.resize((512, 512), Image.LANCZOS)
    tmp = PHOTOS / f"{slug}.tmp.webp"
    im.save(tmp, "WEBP", quality=80, method=6)
    tmp.replace(PHOTOS / f"{slug}.webp")  # атомарно — читатель не увидит частичный файл


def generate(slug: str, title: str, timeout: int = 180) -> bool:
    """Сгенерировать фото блюда (идемпотентно, с защитой от двойной генерации)."""
    if has_photo(slug):
        return True
    lock = _lock_for(slug)
    if not lock.acquire(blocking=False):
        return False  # уже кто-то генерит этот slug
    try:
        if has_photo(slug):
            return True
        key = _key()
        if not key:
            return False
        proxies = None
        p = os.getenv("LLM_PROXY", "").strip()   # RU-прод: OpenRouter гео-блок → через прокси
        if p:
            proxies = {"http": p, "https": p}
        hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://mynutriplan.ru", "X-Title": "NutriPlan dish"}
        body = {"model": IMAGE_MODEL, "modalities": ["image", "text"],
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": _STYLE.format(title=(title or slug.replace('-', ' ')))}]}]}
        r = requests.post(OPENROUTER_URL, headers=hdr, json=body, timeout=timeout, proxies=proxies)
        r.raise_for_status()
        for img in (r.json()["choices"][0]["message"].get("images") or []):
            u = (img.get("image_url") or {}).get("url", "")
            if u.startswith("data:"):
                _save_webp(slug, base64.b64decode(u.split(",", 1)[1]))
                return True
        return False
    except Exception:  # noqa: BLE001 — генерация «best effort», не должна ломать запрос
        return False
    finally:
        lock.release()
