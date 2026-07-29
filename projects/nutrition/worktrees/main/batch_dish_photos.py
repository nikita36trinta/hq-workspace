#!/usr/bin/env python3
"""Батч-генерация фото для всех блюд каталога (единоразово, идемпотентно).

Читает dishes.json, генерит недостающие фото в DATA_DIR/dishphotos с параллелизмом.
ENV: OPENROUTER_API_KEY (+ LLM_PROXY на RU). DATA_DIR — куда складывать.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import dish_photos as dp

WORKERS = 6


def main():
    dishes = json.load(open("dishes.json", encoding="utf-8"))["dishes"]
    todo = [d for d in dishes if not dp.has_photo(d["slug"])]
    print(f"каталог: {len(dishes)}, уже есть: {len(dishes) - len(todo)}, к генерации: {len(todo)}")
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(dp.generate, d["slug"], d["title"]): d for d in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            d = futs[fut]
            good = False
            try:
                good = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! {d['slug']}: {e}")
            ok += good
            fail += not good
            mark = "✓" if good else "×"
            print(f"[{i}/{len(todo)}] {mark} {d['title']}")
            sys.stdout.flush()
    print(f"\nготово: успех {ok}, неудач {fail}, всего фото {sum(1 for d in dishes if dp.has_photo(d['slug']))}/{len(dishes)}")


main()
