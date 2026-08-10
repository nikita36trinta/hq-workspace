"""Вычистить накрутку из журнала аналитики.

Правила выбраны по ДОКАЗАТЕЛЬСТВУ — сверке с кликами в Директе, а не по виду
сессии. Иначе легко выкинуть живого человека, который зашёл и сразу ушёл.

1. 10.08 до 10:50 UTC — кампании стояли на паузе (State=SUSPENDED), кликнуть
   было негде. Всё, что пришло с рекламной меткой в это время, — подделка.
2. 08.08 — по данным Директа кликов в этот день не было вовсе.
3. 09.08 — кликов 99, а сессий 170. Разделить пофамильно нельзя, поэтому
   режем по подписи робота: сессия, в которой ровно visit/report_demo_view/
   tracker_blocked и НИ ОДНОГО действия. Часть живых «отказов» уйдёт вместе с
   ними — это осознанный размен: перебор в 71 сессию искажает знаменатель
   конверсии сильнее, чем потеря нескольких мгновенных уходов.

Запуск:  docker exec site-avto python /app/clean_bots.py [--apply]
"""
from __future__ import annotations

import collections
import json
import shutil
import sys
from pathlib import Path

DATA = Path("/app/data")
LOG = DATA / "analytics_events.jsonl"
LAUNCH = "2026-08-10T10:50"          # момент снятия кампаний с паузы (UTC)
EMPTY = {"visit", "report_demo_view", "tracker_blocked", "js_error"}

apply = "--apply" in sys.argv

rows = []
for line in LOG.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass

sess: dict[str, list] = collections.defaultdict(list)
for r in rows:
    sess[r.get("sid")].append(r)

bots: set[str] = set()
why: collections.Counter = collections.Counter()
for sid, evs in sess.items():
    first = min(e.get("ts", "") for e in evs)
    day = first[:10]
    camp = evs[0].get("camp")
    names = {e.get("name") for e in evs}
    if camp and day == "2026-08-10" and first < LAUNCH:
        bots.add(sid); why["10.08: кампании были на паузе"] += 1
    elif camp and day == "2026-08-08":
        bots.add(sid); why["08.08: кликов в Директе не было"] += 1
    elif day == "2026-08-09" and names <= EMPTY:
        bots.add(sid); why["09.08: сессия без единого действия"] += 1

keep = [r for r in rows if r.get("sid") not in bots]
print("сессий: {} · снимаем {} · остаётся {}".format(
    len(sess), len(bots), len(sess) - len(bots)))
for k, v in sorted(why.items()):
    print("   {}: {}".format(k, v))
print("событий: было {} → останется {}".format(len(rows), len(keep)))

live_by_day = collections.Counter()
for sid, evs in sess.items():
    if sid not in bots:
        live_by_day[min(e.get("ts", "") for e in evs)[:10]] += 1
print("живых сессий по дням:", dict(sorted(live_by_day.items())))

if not apply:
    print("\n(пробный прогон; применить — с ключом --apply)")
    raise SystemExit

shutil.copy2(LOG, str(LOG) + ".bak")
with LOG.open("w", encoding="utf-8") as f:
    for r in keep:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
print("\nжурнал переписан, копия рядом (.bak)")

# Счётчик назначений A/B считал те же визиты — пересобираем по живым,
# СОХРАНЯЯ структуру файла (__since__, ad/organic, by_camp).
ab = DATA / "ab_assign.json"
if ab.exists():
    old = json.loads(ab.read_text(encoding="utf-8"))
    shutil.copy2(ab, str(ab) + ".bak")
    src_c: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    camp_c: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    seen: set[str] = set()
    for r in keep:
        sid = r.get("sid")
        arm = r.get("ab")
        if not sid or not arm or sid in seen:
            continue
        seen.add(sid)
        src_c[r.get("src") or "organic"][arm] += 1
        if r.get("camp"):
            camp_c[r["camp"]][arm] += 1
    new = {"__since__": old.get("__since__")}
    for s in ("ad", "organic"):
        new[s] = dict(src_c.get(s, {}))
    new["by_camp"] = {k: dict(v) for k, v in camp_c.items()}
    ab.write_text(json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print("ab_assign пересобран:", json.dumps(new, ensure_ascii=False))
