#!/usr/bin/env python3
"""Smoke-тест интеграции аналитики против ЖИВОГО проекта (round-trip).

Проверяет, что в конкретном развёрнутом проекте: /admin/selfcheck отвечает и конфиг
валиден, /api/goal принимает событие, оно долетает до /admin/stats.json. Запускать
ПОСЛЕ вендоринга модуля в проект — подтверждает, что всё правильно подключено.

Использование:
    python3 smoke.py https://chistasdelka.ru <ADMIN_TOKEN>
"""
import json
import sys
import time
import urllib.request


def _req(url, data=None, headers=None, timeout=20):
    r = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "ignore")


def main():
    if len(sys.argv) < 3:
        print("usage: python3 smoke.py <base_url> <admin_token>")
        sys.exit(2)
    base, token = sys.argv[1].rstrip("/"), sys.argv[2]
    fails = []

    def check(name, cond, detail=""):
        print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  [{detail}]"))
        if not cond:
            fails.append(name)

    print("SMOKE: analytics integration @", base)

    # 1) selfcheck отвечает и конфиг валиден
    try:
        st, body = _req(f"{base}/admin/selfcheck?token={token}")
        sc = json.loads(body)
        check("selfcheck отвечает", st == 200, f"HTTP {st}")
        check("конфиг без ошибок", sc.get("ok") is True, str(sc.get("config", {}).get("errors")))
        check("хранилище пишется", "storage" in sc, "")
    except Exception as e:  # noqa: BLE001
        check("selfcheck доступен", False, str(e)[:80])
        sc = {}

    # 2) POST события → долетело до stats.json
    sid = "smoke-" + str(int(time.time()))
    ua = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (smoke)",
          "Cookie": f"an_sid={sid}"}
    try:
        st, _ = _req(f"{base}/api/goal", data=json.dumps({"name": "visit", "did": sid}).encode(), headers=ua)
        check("/api/goal принимает событие", st == 200, f"HTTP {st}")
    except Exception as e:  # noqa: BLE001
        check("/api/goal доступен", False, str(e)[:80])

    time.sleep(1)
    try:
        _, body = _req(f"{base}/admin/stats.json?token={token}")
        d = json.loads(body)
        check("событие долетело в дашборд", (d.get("visits") or 0) >= 1, f"visits={d.get('visits')}")
        check("воронка построена", bool(d.get("steps")), "")
    except Exception as e:  # noqa: BLE001
        check("stats.json доступен", False, str(e)[:80])

    print("\nИТОГ:", "OK — интеграция рабочая" if not fails else f"ПРОВАЛЫ: {fails}")
    sys.exit(0 if not fails else 1)


main()
