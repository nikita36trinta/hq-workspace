"""Подсветка повреждённых зон на готовом снимке машины (image-to-image).

Отдаём модели наш же рендер и просим закрасить нужные панели кузова, не трогая
всё остальное. Так подсветка ложится ПО ФОРМЕ двери, с изломами по стыкам, —
наложением полупрозрачного прямоугольника в CSS этого не получить.

Запускать внутри боевого контейнера (там ключ и прокси):
    docker exec site-avto python /app/gen_car_edit.py
"""
from __future__ import annotations

import base64
import mimetypes
import os
import sys
from pathlib import Path

import httpx

URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("CAR_IMAGE_MODEL", "google/gemini-3-pro-image")
CARS = Path("/app/static/cars")

SRC = CARS / "x1-side.png"          # исходный рендер в полном разрешении

# Два прочтения одной задачи: заливка панели и обводка по контуру. Какое
# читается лучше на маленькой карточке — видно только глазами, поэтому оба.
EDITS = {
    # Первый прогон этой формулировки вернул ответ без картинки — молча, без
    # ошибки. Модель так делает, когда правка кажется ей слишком общей; помогает
    # назвать цвет конкретно и сказать, что именно остаётся видимым.
    "x1-side-doors-bright": (
        "Using the provided photograph of the silver BMW X1, paint BOTH side doors "
        "(front door and rear door, metal panels below the window line) with a vivid "
        "semi-transparent ORANGE overlay, colour #E8820C at about 55% opacity, to mark "
        "them as the damaged area. The orange must cover the door panels exactly, "
        "stopping cleanly at the real panel seams: the shut lines, the window belt line "
        "and the bottom sill. Door handles and the body's highlights and reflections "
        "must remain visible through the orange. Do not colour the fenders, the roof, "
        "the glass, the bumpers or the wheels. Everything else — car position, "
        "lighting, shadow, white background — stays exactly as in the original. "
        "No text, no arrows, no labels, no watermark."
    ),
    "x1-side-doors": (
        "Using the provided photograph of the silver BMW X1, mark BOTH side doors "
        "(the front door and the rear door) as damaged by overlaying them with a "
        "translucent warm amber-orange colour wash. The wash must follow the exact "
        "shape of each door panel, with crisp edges that snap to the real door seams, "
        "the window line and the sill — like a highlighter tracing the panel. Keep the "
        "door handles, glass and body reflections visible through the colour. "
        "Everything else in the image — the car's position, the wheels, the bumpers, "
        "the roof, the lighting, the shadow and the white background — must stay "
        "exactly as in the original, completely untouched. No text, no arrows, no "
        "labels, no watermark."
    ),
    "x1-side-doors-outline": (
        "Using the provided photograph of the silver BMW X1, outline BOTH side doors "
        "(front and rear) with a bright amber-orange contour line about 6 pixels thick "
        "that follows the real door seams precisely, and fill the enclosed door panels "
        "with a very light translucent amber tint so the metal and reflections remain "
        "clearly visible. Everything else in the image — car position, wheels, bumpers, "
        "roof, lighting, shadow, white background — must remain exactly as in the "
        "original. No text, no arrows, no labels, no watermark."
    ),
}


def main() -> int:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("нет OPENROUTER_API_KEY", file=sys.stderr)
        return 1
    if not SRC.exists():
        print(f"нет исходника {SRC}", file=sys.stderr)
        return 1
    proxy = os.getenv("LLM_PROXY", "").strip() or None

    raw = SRC.read_bytes()
    # Расширение врёт (генератор отдал JPEG в файле .png) — определяем по сигнатуре.
    mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
    data_uri = f"data:{mime};base64," + base64.b64encode(raw).decode()
    print(f"исходник: {SRC.name}, {len(raw)//1024} КБ, {mime}")

    for name, prompt in EDITS.items():
        dest = CARS / f"{name}.png"
        if dest.exists():
            print(f"{name}: уже есть, пропускаем")
            continue
        body = {"model": MODEL, "modalities": ["image", "text"],
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ]}]}
        hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "HTTP-Referer": "https://avto.chistasdelka.ru", "X-Title": "avto damage"}
        try:
            with httpx.Client(proxy=proxy, timeout=300) as c:
                r = c.post(URL, headers=hdr, json=body)
            if r.status_code >= 400:
                print(f"{name}: ОТКАЗ {r.status_code} {r.text[:300]}", file=sys.stderr)
                continue
            msg = r.json()["choices"][0]["message"]
            for img in (msg.get("images") or []):
                u = (img.get("image_url") or {}).get("url", "")
                if u.startswith("data:"):
                    dest.write_bytes(base64.b64decode(u.split(",", 1)[1]))
                    print(f"{name}: сохранено ({dest.stat().st_size//1024} КБ)")
                    break
            else:
                print(f"{name}: картинки в ответе нет — {str(msg)[:200]}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
