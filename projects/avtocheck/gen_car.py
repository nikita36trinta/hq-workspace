"""Рендеры машины для карточки первого экрана — Nano Banana Pro через OpenRouter.

Запускать ВНУТРИ боевого контейнера: там лежат OPENROUTER_API_KEY и LLM_PROXY
(из РФ OpenRouter отвечает гео-блоком, без прокси запрос не уходит).

    docker exec site-avto python /app/gen_car.py

Кладёт файлы в /app/static/cars/. Идемпотентно: уже существующий файл не
перегенерирует — повторный запуск не стоит денег.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("CAR_IMAGE_MODEL", "google/gemini-3-pro-image")
OUT = Path("/app/static/cars")

# Общий хвост промпта. Белый фон и полное отсутствие подписей обязательны:
# картинку мы кладём на светлую карточку и рисуем поверх неё свои метки зон.
TAIL = (
    "Isolated on a pure white seamless background. Soft, even studio lighting with a "
    "subtle contact shadow. Sharp focus, high detail, photorealistic. The entire car is "
    "fully inside the frame with comfortable margins on all sides. No text, no captions, "
    "no watermarks, no logos overlaid, no people, no background objects."
)

SHOTS = {
    "x1-side": (
        "Photorealistic studio product photograph of a silver metallic BMW X1 (F48) "
        "compact crossover SUV. Exact side profile view: the camera is perpendicular to "
        "the car at mid-height, so the body is seen at a true 90 degrees with no "
        "perspective distortion. Front of the car points to the left. Wheels straight. "
        + TAIL
    ),
    "x1-top": (
        "Photorealistic 3D product render of a silver metallic BMW X1 (F48) compact "
        "crossover SUV seen from directly above — a strict top-down bird's eye view of "
        "the roof, hood and trunk, with the wheels just visible at the sides. The front "
        "of the car points to the left. Panoramic glass roof and windscreen appear dark. "
        + TAIL
    ),
}


def main() -> int:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("нет OPENROUTER_API_KEY", file=sys.stderr)
        return 1
    proxy = os.getenv("LLM_PROXY", "").strip() or None
    OUT.mkdir(parents=True, exist_ok=True)

    for name, prompt in SHOTS.items():
        dest = OUT / f"{name}.png"
        if dest.exists():
            print(f"{name}: уже есть ({dest.stat().st_size} байт), пропускаем")
            continue
        body = {"model": MODEL, "modalities": ["image", "text"],
                "messages": [{"role": "user",
                              "content": [{"type": "text", "text": prompt}]}]}
        hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://avto.chistasdelka.ru", "X-Title": "avto hero"}
        try:
            with httpx.Client(proxy=proxy, timeout=300) as c:
                r = c.post(URL, headers=hdr, json=body)
            if r.status_code >= 400:
                print(f"{name}: ОТКАЗ {r.status_code} {r.text[:300]}", file=sys.stderr)
                continue
            msg = r.json()["choices"][0]["message"]
            saved = False
            for img in (msg.get("images") or []):
                u = (img.get("image_url") or {}).get("url", "")
                if u.startswith("data:"):
                    dest.write_bytes(base64.b64decode(u.split(",", 1)[1]))
                    print(f"{name}: сохранено {dest} ({dest.stat().st_size} байт)")
                    saved = True
                    break
            if not saved:
                print(f"{name}: картинки в ответе нет — {str(msg)[:200]}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
