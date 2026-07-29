#!/usr/bin/env python3
"""
Генератор поз маскота-авокадо (тот же персонаж, что в avocado_panel.mp4).

Берём кадр-референс avocado_ref.png и через Nano Banana (Gemini image, OpenRouter)
генерим позы image-to-image → консистентный персонаж. Фон белый → вырезаем в
прозрачный PNG (rembg), чтобы маскот ложился на любую тему квиза.

  python gen_mascot.py --list
  python gen_mascot.py --only avocado_wave
  python gen_mascot.py                 # все, чего ещё нет
  python gen_mascot.py --cut avocado_wave   # только вырезать
"""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path

import requests

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
IMAGE_MODEL = os.getenv("NB_IMAGE_MODEL", "google/gemini-3.1-flash-image")
ASSETS = Path(__file__).parent / "static" / "assets"
REF = ASSETS / "avocado_ref.png"

STYLE = (
    "Soft 3D render of the SAME cute chibi avocado mascot character shown in the reference image — "
    "matte clay-like smooth material, plump pear-shaped avocado body, big round brown glossy pit as a belly, "
    "a small sprout with two little green leaves on top of the head, big cute glossy dark eyes, soft pink blush "
    "cheeks, a tiny gentle smile, little stub arms and little feet. {pose}. Full body, centered, "
    "on the SAME soft solid pastel mint-green background as the reference image, soft studio lighting, "
    "gentle soft contact shadow, adorable friendly brand mascot, high detail. Keep the EXACT same character "
    "design, proportions, material and colors as the reference image. No text, no watermark, no extra objects "
    "unless described. IMPORTANT: render exactly ONE single avocado character, one full-body figure only — "
    "NOT a grid, NOT a collage, NOT a sprite sheet, NOT multiple copies or duplicates."
)

POSES = {
    "avocado_wave":      "raising one little arm to wave a friendly hello, warm happy welcoming smile, looking at the viewer",
    "avocado_ask":       "holding a small tablet with a checklist in its arms, curious friendly attentive look as if asking a question",
    "avocado_think":     "one little arm raised to its cheek in a thoughtful pondering pose, looking up slightly, curious and cute",
    "avocado_cheer":     "giving an encouraging thumbs up with one arm, big proud happy smile, looking at the viewer",
    "avocado_celebrate": "one single avocado standing, both its little arms raised up high in joyful celebration, big excited delighted open smile",
    "avocado_run":       "in a dynamic cute running and jogging pose, energetic cheerful expression, a little sweat drop, active",
    "avocado_chef":      "wearing a tiny white chef's toque hat and holding a small wooden cooking spoon, cheerful cooking pose",
    "avocado_love":      "gently holding a small soft red heart shape in its little arms, warm caring tender smile",
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


def _ref_data_uri() -> str:
    b = REF.read_bytes()
    return "data:image/png;base64," + base64.b64encode(b).decode()


def generate(name: str, pose: str) -> None:
    prompt = STYLE.format(pose=pose)
    hdr = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json",
           "HTTP-Referer": "http://localhost/nutrition", "X-Title": "Nutrition mascot"}
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _ref_data_uri()}},
    ]
    body = {"model": IMAGE_MODEL, "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": content}]}
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


def cut(name: str) -> None:
    """Вырезать маскота со светлого фона → прозрачный {name}_cut.png.

    Фон = пиксели с низкой насыщенностью (бело-серые) И связанные с краем кадра.
    Так убираем и белый фон, и мягкую серую тень, но НЕ трогаем внутренние блики
    в глазах. scipy.ndimage — без numba.
    """
    import numpy as np
    from PIL import Image, ImageFilter
    from scipy import ndimage
    src = ASSETS / f"{name}.png"
    if not src.exists():
        print(f"✗ нет {src}"); return
    a = np.asarray(Image.open(src).convert("RGB")).astype(np.int16)
    mx = a.max(2); mn = a.min(2)
    bg_cand = ((mx - mn) < 24) & (mx > 130)          # низкая насыщенность + светлый
    lbl, _ = ndimage.label(bg_cand)
    border = np.concatenate([lbl[0, :], lbl[-1, :], lbl[:, 0], lbl[:, -1]])
    keep = [int(x) for x in np.unique(border) if x != 0]
    bg = np.isin(lbl, keep) if keep else np.zeros_like(bg_cand)
    alpha = Image.fromarray(np.where(bg, 0, 255).astype("uint8")).filter(ImageFilter.GaussianBlur(1.0))
    out = Image.open(src).convert("RGBA"); out.putalpha(alpha)
    bbox = out.getbbox()
    if bbox:
        l, t, r, b = bbox; pad = 8
        out = out.crop((max(0, l - pad), max(0, t - pad), min(out.width, r + pad), min(out.height, b + pad)))
    out.save(ASSETS / f"{name}_cut.png")
    print(f"✓ {name}_cut.png ({out.width}×{out.height})")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", nargs="*")
    p.add_argument("--cut", nargs="*")
    p.add_argument("--list", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--nocut", action="store_true", help="не вырезать после генерации")
    a = p.parse_args()
    if a.list:
        for k in POSES:
            print(k)
        return
    if not REF.exists():
        raise SystemExit(f"нет референса {REF} — извлеки кадр из avocado_panel.mp4")
    if a.cut:
        for name in a.cut:
            cut(name)
        return
    for name in (a.only or list(POSES)):
        if name not in POSES:
            print(f"? неизвестный: {name}"); continue
        if (ASSETS / f"{name}.png").exists() and not a.force:
            print(f"· {name}.png уже есть");
        else:
            try:
                generate(name, POSES[name])
            except Exception as e:  # noqa: BLE001
                print(f"✗ {name}: {e}"); continue
        if not a.nocut:
            try:
                cut(name)
            except Exception as e:  # noqa: BLE001
                print(f"✗ cut {name}: {e}")


if __name__ == "__main__":
    main()
