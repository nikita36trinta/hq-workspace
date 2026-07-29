#!/usr/bin/env python3
"""Оживить позы-стиллы авокадо в короткие mp4-лупы через Kling i2v (Fal).

Переиспользует provider.fal_image_to_video из mascot-factory. ~$0.28 за 5с/позу.
  python animate_mascot.py                  # все позы, которых ещё нет
  python animate_mascot.py --only wave run
  python animate_mascot.py --force
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

MF = Path("/Users/Nikita/Desktop/digiterium/hq-workspace/brand/mascot-factory")
sys.path.insert(0, str(MF))
import provider  # noqa: E402

ASSETS = Path(__file__).parent / "static" / "assets"
TAIL = (" Keep the EXACT same premium soft 3D clay render, smooth rounded plush look, same character, same "
        "colors and same background. Extremely smooth, gentle, subtle. No morphing, no camera movement, "
        "seamless gentle loop.")

MOTION = {
    "wave":      "The cute avocado waves its raised little arm side to side in a friendly hello, gentle idle bob, one slow cute blink." + TAIL,
    "ask":       "The cute avocado gently breathes and bobs, gives one slow cute blink and a tiny curious happy nod, holding its little tablet steady." + TAIL,
    "think":     "The cute avocado tilts its head slightly in thought, one slow cute blink, gentle idle bob, little arm resting on its cheek." + TAIL,
    "cheer":     "The cute avocado does a happy little bounce, keeps its thumbs up steady, one slow cute blink, cheerful." + TAIL,
    "celebrate": "The cute avocado does a joyful gentle bounce with both little arms raised up, one slow cute blink, happy excited wiggle." + TAIL,
    "run":       "The cute avocado runs happily in place in a cute loop, little legs and arms gently pumping, energetic, gentle bob." + TAIL,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--duration", default="5")
    a = ap.parse_args()
    names = a.only or list(MOTION)
    for n in names:
        if n not in MOTION:
            print(f"? неизвестная поза: {n}", flush=True); continue
        still = ASSETS / f"avocado_{n}.png"
        out = ASSETS / f"avocado_{n}.mp4"
        if not still.exists():
            print(f"✗ нет стилла {still}", flush=True); continue
        if out.exists() and not a.force:
            print(f"· {out.name} уже есть", flush=True); continue
        print(f"→ Kling i2v: {n} …", flush=True)
        try:
            mp4 = provider.fal_image_to_video(still.read_bytes(), MOTION[n], duration=a.duration)
            out.write_bytes(mp4)
            print(f"✓ {out.name} ({len(mp4)//1024} KB)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"✗ {n}: {e}", flush=True)


if __name__ == "__main__":
    main()
