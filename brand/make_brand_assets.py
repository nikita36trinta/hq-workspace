#!/usr/bin/env python3
"""
Пайплайн бренд-ассетов для лендинг-MVP.

Один источник правды по логотипам всех сайтов. На выходе:
  • favicon.svg  → раскладывается в static/ каждого проекта (иконка во вкладке)
  • avatar_<product>_512.png → 512×512, full-bleed (Telegram круг-кроп) —
    аватар для бота (заливается вручную через @BotFather → /setuserpic,
    т.к. Bot API не умеет менять аватар самого бота).

Добавить новый сайт → допиши запись в BRANDS и запусти:  python3 make_brand_assets.py
Требует ImageMagick (`magick`/`convert`) для рендера PNG.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HQ = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "out"


def _favicon(bg1: str, bg2: str, emblem: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">'
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{bg1}"/><stop offset="1" stop-color="{bg2}"/>'
        '</linearGradient></defs>'
        '<rect width="64" height="64" rx="15" fill="url(#bg)"/>'
        f'{emblem}</svg>'
    )


def _avatar(bg1: str, bg2: str, emblem: str) -> str:
    # full-bleed 512: фон без скругления (Telegram сам круг-кропит),
    # эмблема из 64-вьюпорта увеличена и центрирована с запасом под круг.
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">'
        f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{bg1}"/><stop offset="1" stop-color="{bg2}"/>'
        '</linearGradient></defs>'
        '<rect width="512" height="512" fill="url(#bg)"/>'
        f'<g transform="translate(96 96) scale(5)">{emblem}</g></svg>'
    )


# Эмблемы (без фонового прямоугольника — фон рисуют _favicon/_avatar).
SHIELD_CHECK = (
    '<defs><linearGradient id="sh" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#dbe6f5"/>'
    '</linearGradient></defs>'
    '<path d="M32 11 L49 17.5 V32 C49 43.4 41.6 49.7 32 53 C22.4 49.7 15 43.4 15 32 V17.5 Z" fill="url(#sh)"/>'
    '<path d="M24 32.5 l6 6 L42 25.5" fill="none" stroke="#2563EB" stroke-width="5" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
)

DOC_SEAL = (
    '<defs><linearGradient id="seal" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#d3742f"/><stop offset="1" stop-color="#b4551f"/>'
    '</linearGradient></defs>'
    '<rect x="16" y="11" width="27" height="35" rx="3.5" fill="#f6efe1"/>'
    '<rect x="20.5" y="18" width="18" height="2.6" rx="1.3" fill="#c3b49a"/>'
    '<rect x="20.5" y="24" width="18" height="2.6" rx="1.3" fill="#c3b49a"/>'
    '<rect x="20.5" y="30" width="12" height="2.6" rx="1.3" fill="#c3b49a"/>'
    '<circle cx="43" cy="44" r="11" fill="url(#seal)" stroke="#211c15" stroke-width="3"/>'
    '<circle cx="43" cy="44" r="7.4" fill="none" stroke="#f2c79b" stroke-width="1.4" opacity=".7"/>'
    '<path d="M43 39.4 l1.5 3 3.3.5 -2.4 2.3 .6 3.3 -3-1.6 -3 1.6 .6-3.3 -2.4-2.3 3.3-.5 Z" '
    'fill="#f6efe1" opacity=".92"/>'
)

BRANDS = {
    "sdelka": {
        "static": HQ / "projects/sdelka/worktrees/main/static",
        "bg": ("#173768", "#0f2440"), "emblem": SHIELD_CHECK,
    },
    "nasledstvo": {
        "static": HQ / "projects/nasledstvo/worktrees/main/static",
        "bg": ("#312a20", "#211c15"), "emblem": DOC_SEAL,
    },
}


CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def _render_png(svg_path: Path, png_path: Path) -> bool:
    # Chrome headless рендерит SVG корректно (ImageMagick без librsvg — нет).
    chrome = next((p for p in CHROME_PATHS if Path(p).exists()), None) \
        or shutil.which("chromium") or shutil.which("google-chrome")
    if chrome:
        r = subprocess.run([
            chrome, "--headless", "--disable-gpu", "--force-device-scale-factor=1",
            f"--screenshot={png_path}", "--window-size=512,512",
            "--default-background-color=00000000", f"file://{svg_path}",
        ], capture_output=True)
        if png_path.exists() and png_path.stat().st_size > 2000:
            return True
    tool = shutil.which("rsvg-convert")
    if tool:
        return subprocess.run([tool, "-w", "512", "-h", "512", "-o", str(png_path),
                               str(svg_path)], capture_output=True).returncode == 0
    return False


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, b in BRANDS.items():
        bg1, bg2 = b["bg"]
        fav = _favicon(bg1, bg2, b["emblem"])
        avatar = _avatar(bg1, bg2, b["emblem"])
        # favicon → в проект + в out/
        (b["static"]).mkdir(parents=True, exist_ok=True)
        (b["static"] / "favicon.svg").write_text(fav, encoding="utf-8")
        (OUT / f"favicon_{name}.svg").write_text(fav, encoding="utf-8")
        av_svg = OUT / f"avatar_{name}_512.svg"
        av_svg.write_text(avatar, encoding="utf-8")
        ok = _render_png(av_svg, OUT / f"avatar_{name}_512.png")
        print(f"  {name}: favicon → {b['static'].name}/favicon.svg | avatar PNG: {'ok' if ok else 'НЕТ (нет ImageMagick)'}")
    print(f"\nАватары ботов: {OUT}/avatar_<product>_512.png")
    print("Залить аватар: @BotFather → /setuserpic → выбрать бота → отправить PNG")
    return 0


if __name__ == "__main__":
    sys.exit(main())
