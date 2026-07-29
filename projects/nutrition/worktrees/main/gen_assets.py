#!/usr/bin/env python3
"""
Генератор контент-картинок сайта через Nano Banana 2 (Gemini image via OpenRouter).

Наполнение лендингов: спорт-девушка в hero, фото еды, lifestyle-люди, ингредиенты для
build-секций и т.д. Подписи КБЖУ/ккал НЕ запекаем — накладываем в HTML.

Ключ OPENROUTER_API_KEY подхватывается из .hq/pipelines/.env.
Модерация: аспирация/lifestyle, БЕЗ обещаний «минус N кг» и медицины.

Примеры:
  python gen_assets.py --list
  python gen_assets.py --only fitness_girl food_bowl
  python gen_assets.py            # всё, чего ещё нет
"""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGE_MODEL = os.getenv("NB_IMAGE_MODEL", "google/gemini-3.1-flash-image")  # Nano Banana 2
ASSETS = Path(__file__).parent / "static" / "assets"

# Единый стиль всех фото — чтобы сайт смотрелся цельно.
STYLE = ("premium editorial lifestyle photography, soft natural daylight, bright airy, clean, "
         "shallow depth of field, warm off-white and fresh green palette, high detail, appetizing, "
         "modern healthy food brand aesthetic, no text, no watermark. ")

MANIFEST = {
    # ── hero-люди ──
    "fitness_girl": "A fit athletic young woman, WAIST-UP portrait, HAIR IN A NEAT TIDY LOW BUN tied back "
                    "with NO loose strands or flyaways (clean hairline for easy cutout), wearing stylish "
                    "modern green activewear, smiling warmly, holding a fresh colorful healthy grain bowl in "
                    "front of her, on a PLAIN SOLID SEAMLESS light grey studio backdrop. Professional studio "
                    "lighting with a large softbox key light, shot on Canon EOS R5, 85mm f/1.8 lens, shallow "
                    "depth of field, realistic natural skin texture with fine detail and pores, subtle catchlights "
                    "in the eyes, candid authentic expression, PHOTOREALISTIC editorial magazine photograph — "
                    "looks like a real photo, not an illustration or 3D render, no over-smoothing. "
                    "Centered, clean simple silhouette. " + STYLE,
    "lifestyle_slim": "A happy healthy young woman in casual athleisure feeling light and confident, "
                      "stretching by a sunny window in the morning, joyful, vertical composition. " + STYLE,
    "lifestyle_energy": "A cheerful person full of energy in the morning, drinking a green smoothie by a "
                        "bright window, fresh and vibrant, lifestyle, vertical composition. " + STYLE,
    # ── аватары для отзывов (реальные лица) ──
    "avatar_1": "Friendly natural headshot portrait of a young athletic woman, warm genuine smile, casual, "
                "plain soft neutral studio background, realistic candid photo shot on 85mm, face centered. " + STYLE,
    "avatar_2": "Friendly natural headshot portrait of a young fit man, warm genuine smile, casual t-shirt, "
                "plain soft neutral studio background, realistic candid photo shot on 85mm, face centered. " + STYLE,
    "avatar_3": "Friendly natural headshot portrait of a woman in her 30s, warm genuine smile, casual, "
                "plain soft neutral studio background, realistic candid photo shot on 85mm, face centered. " + STYLE,
    # ── еда ──
    "food_bowl": "Overhead flat lay of a colorful balanced healthy bowl: grilled chicken, quinoa, avocado, "
                 "cherry tomatoes, fresh greens, on a soft ceramic plate, on a light table. " + STYLE,
    "food_plate": "A beautifully plated healthy dinner: salmon fillet, roasted vegetables, quinoa, on a "
                  "clean modern plate, restaurant quality, top-down. " + STYLE,
    "food_breakfast": "A bright healthy breakfast: greek yogurt with berries, granola, honey, and fresh "
                      "fruit, on a light wooden table, cozy morning light. " + STYLE,
    "food_bowls_trio": "Three different colorful healthy meal bowls in a row on a light surface, variety "
                       "of fresh ingredients, top-down, clean. " + STYLE,
    # ── ингредиенты / build (daily-harvest-style) ──
    "smoothie_ready": "A vibrant green smoothie in a clear glass, fresh, condensation on glass, garnished "
                      "with mint, on a light background, centered. " + STYLE,
    "ingredients_flatlay": "Neat flat lay of fresh smoothie ingredients — spinach, banana, avocado, chia "
                           "seeds, berries — arranged on a light surface, top-down, organized. " + STYLE,

    # ── pro (тёмный атлетик, whoop) — СВОЙ тёмный стиль, без общего STYLE ──
    "pro_athlete": "A strong athletic young woman in sleek matte-black activewear, mid strength workout, powerful "
                   "confident pose, dramatic moody studio lighting with a rim light, deep charcoal near-black "
                   "background, subtle sweat glisten, high-contrast cinematic editorial fitness photography, shot on "
                   "85mm f1.4, realistic skin texture, photorealistic — a real photo not a render, no text, no watermark.",
    "pro_meal": "A high-protein performance meal on a dark slate plate — grilled salmon, soft-boiled eggs, avocado, "
                "greens, quinoa — moody dramatic side lighting, dark charcoal background, faint steam, premium dark "
                "food photography, top-down, ultra appetizing, photorealistic, no text, no watermark.",
    # ── chef (кулинарный журнал / daily-harvest) — тёплый editorial ──
    "chef_hero": "An overhead editorial food-magazine photograph of a beautifully plated wholesome gourmet dish on a "
                 "handmade ceramic plate, warm natural window light, linen napkin, rustic wooden table, generous "
                 "styling, shallow depth of field, appetizing, photorealistic, warm cream and terracotta tones, "
                 "no text, no watermark.",
    "chef_hands": "Close-up of a woman's hands finishing plating a fresh healthy dish in a cozy warm kitchen, soft "
                  "natural window light, authentic candid editorial food photography, warm earthy tones, shallow "
                  "depth of field, photorealistic, no text, no watermark.",
    "chef_flatlay": "A rustic editorial overhead flat lay of fresh whole ingredients — heirloom vegetables, herbs, "
                    "grains, olive oil — on a warm wooden board, natural directional light, magazine styling, "
                    "photorealistic, no text, no watermark.",
    # ── coach (яркий app-коуч, yazio/Noom) — чистый яркий портрет ──
    "coach_hero": "A happy confident young woman in a casual bright outfit smiling while looking at her phone, "
                  "standing against a clean solid vibrant color studio background, joyful energetic lifestyle "
                  "portrait, aspirational, shot on 85mm f1.8, realistic skin, photorealistic — a real photo, "
                  "centered, no text, no watermark.",
    # ── reset (мягкий пастель-wellness, Headspace) ──
    "reset_hero": "A serene calm young woman peacefully enjoying a healthy breakfast by a bright window in soft "
                  "morning light, gentle and mindful, soft pastel lavender and peach tones, wellness lifestyle, "
                  "vertical composition, shallow depth of field, photorealistic, no text, no watermark.",
    "reset_bowl": "A beautiful soft pastel smoothie bowl topped with berries, granola and edible flowers, gentle "
                  "diffused morning light, minimal calm styling on a soft pastel surface, top-down, dreamy, "
                  "appetizing, photorealistic, no text, no watermark.",
}


def _key() -> str:
    if os.getenv("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    for up in [Path(__file__).resolve().parents[i] for i in range(2, 7)]:
        env = up / ".hq" / "pipelines" / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("OPENROUTER_API_KEY="):
                    return s.split("=", 1)[1].strip()
    raise SystemExit("нет OPENROUTER_API_KEY")


def generate(name: str, prompt: str) -> None:
    hdr = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
           "HTTP-Referer": "http://localhost/nutrition", "X-Title": "Nutrition assets"}
    body = {"model": IMAGE_MODEL, "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}]}
    r = requests.post(OPENROUTER_URL, headers=hdr, json=body, timeout=180)
    r.raise_for_status()
    for img in (r.json()["choices"][0]["message"].get("images") or []):
        url = (img.get("image_url") or {}).get("url", "")
        if url.startswith("data:"):
            ASSETS.mkdir(parents=True, exist_ok=True)
            (ASSETS / f"{name}.png").write_bytes(base64.b64decode(url.split(",", 1)[1]))
            print(f"✓ {name}.png")
            return
    raise RuntimeError(f"{name}: модель не вернула изображение")


def cut_person(name: str) -> None:
    """Вырезать человека/объект из фона (rembg, как на growfood) → {name}_cut.png.

    Для ФОТО людей работает чисто (в отличие от soft-3D-рендера — там свет запекается).
    Заливаем внутренние дыры + мягкий край.
    """
    import numpy as np
    from collections import deque
    from PIL import Image, ImageFilter
    from rembg import remove, new_session
    src = ASSETS / f"{name}.png"
    if not src.exists():
        print(f"✗ нет {src}"); return
    # isnet-general — вырезает ВЕСЬ салиентный субъект (человек + боул в руках), а не только тело;
    # u2net_human_seg выкидывает предмет в руках. Реальный фон (проём рука-бедро) остаётся прозрачным.
    sess = new_session("isnet-general-use")
    rgb = Image.open(src).convert("RGB")
    mask = np.array(remove(rgb, session=sess, only_mask=True).convert("L")).astype(np.float32)
    # мягкий край + чистый фон; НЕ заливаем внутренние «кольца» — область между рукой (упёртой
    # в бок) и телом должна остаться прозрачной. Маска u2net-human-seg сохраняет топологию.
    a = np.clip((mask - 25) * (255.0 / 175.0), 0, 255).astype(np.uint8)
    alpha = Image.fromarray(a).filter(ImageFilter.GaussianBlur(0.6))
    out = rgb.convert("RGBA"); out.putalpha(alpha)
    bbox = out.getbbox()  # обрезаем прозрачные поля → фигура вплотную к краям (для 3D-вылезания)
    if bbox:
        l, t, r, b = bbox
        pad = 6
        out = out.crop((max(0, l - pad), max(0, t - pad),
                        min(out.width, r + pad), min(out.height, b + pad)))
    out.save(ASSETS / f"{name}_cut.png")
    print(f"✓ {name}_cut.png (вырезан, {out.width}×{out.height})")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", nargs="*", help="какие ассеты генерить")
    p.add_argument("--cut", nargs="*", help="вырезать людей/объекты из фона (rembg)")
    p.add_argument("--list", action="store_true")
    p.add_argument("--force", action="store_true", help="перегенерить существующие")
    a = p.parse_args()
    if a.list:
        for k in MANIFEST:
            print(k)
        return
    if a.cut:
        for name in a.cut:
            cut_person(name)
        return
    names = a.only or list(MANIFEST)
    for name in names:
        if name not in MANIFEST:
            print(f"? неизвестный: {name}"); continue
        if (ASSETS / f"{name}.png").exists() and not a.force:
            print(f"· {name}.png уже есть"); continue
        try:
            generate(name, MANIFEST[name])
        except Exception as e:  # noqa: BLE001
            print(f"✗ {name}: {e}")


if __name__ == "__main__":
    main()
