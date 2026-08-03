"""Проектные панели дашборда NutriPlan.

Модуль аналитики намеренно агностичен: он считает воронку, деньги и матрицу
кампаний, а всё, что знает только проект, отдаётся ему готовыми панелями. Здесь
живёт именно это — срезы «кто и откуда», тарифы и здоровье выдачи.

Почему срезы вообще нужны. Воронка без них — одно число: «до оплаты дошло 3%».
Ни починить, ни улучшить по нему нечего. Тот же 3% в разрезе устройства обычно
оказывается 5% на десктопе и 1% на телефоне, и это уже задача с решением.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

# Ступени, по которым режем каждый срез. Меньше, чем полная воронка: таблица на
# восемь колонок не читается, а эти три отвечают на вопрос «где теряем».
STEPS = [("quiz_start", "Начали квиз"), ("lead", "Оставили почту"), ("pay_click", "К оплате")]


def _events(data_dir: str, since: str = "") -> list[dict]:
    f = Path(data_dir) / "analytics_events.jsonl"
    if not f.exists():
        return []
    out = []
    try:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if since and (r.get("ts") or "") < since:
                continue
            out.append(r)
    except Exception:  # noqa: BLE001
        return []
    return out


def _slice(events: list[dict], field: str, title: str, labels: dict | None = None) -> dict | None:
    """Одна таблица-срез: значение поля → уники по ступеням.

    Считаем УНИКИ по sid, а не события: человек, трижды открывший пейволл, —
    один человек. По событиям конверсия срезов выходила бы больше 100%.
    """
    by: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for e in events:
        v = (e.get(field) or "").strip()
        if not v:
            continue
        sid = e.get("sid") or ""
        if not sid:
            continue
        by[v]["_all"].add(sid)
        by[v][e.get("name") or ""].add(sid)
    if not by:
        return None
    rows = []
    for v, agg in sorted(by.items(), key=lambda kv: -len(kv[1]["_all"])):
        base = len(agg["_all"])
        cells = [labels.get(v, v) if labels else v, base]
        prev = base
        for name, _ in STEPS:
            n = len(agg.get(name, ()))
            cells += [n, f"{n / prev * 100:.0f}%" if prev else "—"]
            prev = n or prev
        rows.append(cells)
    head = ["Значение", "Сессий"]
    for _, lbl in STEPS:
        head += [lbl, "→"]
    return {"title": f"Срез: {title}", "tag": f"{len(rows)} значений",
            "table": {"head": head, "rows": rows}}


def _tariffs(events: list[dict], payments: list[dict]) -> dict:
    """Что выбирают на пейволле и что из этого оплачивают.

    Клики по тарифу живут в событиях, деньги — в журнале заказов; сводим, потому
    что порознь они отвечают на половину вопроса. «Подписку выбирают вдвое чаще»
    ничего не стоит, если платят по ней вдвое реже.
    """
    clicks = defaultdict(set)
    for e in events:
        n = e.get("name")
        if n in ("pay_click_sub", "pay_click_once") and e.get("sid"):
            clicks["sub" if n.endswith("_sub") else "once"].add(e["sid"])
    money = defaultdict(lambda: {"n": 0, "rev": 0.0})
    for p in payments:
        k = "sub" if (p.get("method") or "").startswith("sub") else "once"
        money[k]["n"] += 1
        money[k]["rev"] += float(p.get("amount") or 0)
    rows = []
    for k, name in (("sub", "Подписка 499 ₽/мес"), ("once", "Разовый план 299 ₽")):
        c, m = len(clicks.get(k, ())), money.get(k, {"n": 0, "rev": 0.0})
        rows.append([name, c, m["n"], f"{m['n'] / c * 100:.0f}%" if c else "—",
                     f"{round(m['rev'])} ₽"])
    return {"title": "Тарифы", "tag": "выбор → оплата",
            "table": {"head": ["Тариф", "Выбрали", "Оплатили", "CVR", "Выручка"], "rows": rows}}


def _steps_of(events: list[dict]) -> list[tuple[int, str]]:
    """Шаги квиза в порядке прохождения. Берём из самих событий, а не из списка
    в коде: квиз меняется, а дашборд не должен требовать правки следом."""
    seen: dict[int, str] = {}
    for e in events:
        if e.get("name") != "quiz_step":
            continue
        raw = str(e.get("part") or "")
        num, _, title = raw.partition("|")
        try:
            seen.setdefault(int(num), title or f"шаг {num}")
        except ValueError:
            continue
    return sorted(seen.items())


def _depth(events: list[dict], steps: list[tuple[int, str]]) -> dict | None:
    """Насколько глубоко проходят квиз — в разрезе лендинга.

    Отвечает на вопрос, который не виден в воронке: «начали 40%, дошли до почты
    12%» не говорит, ушли люди на втором вопросе или на десятом. Средний шаг и
    доля пройденного показывают, лендинг привёл не тех или квиз слишком длинный.
    """
    if not steps:
        return None
    total = len(steps)
    reach: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        if e.get("name") != "quiz_step":
            continue
        sid, ab = e.get("sid"), (e.get("ab") or "—")
        if not sid:
            continue
        try:
            n = int(str(e.get("part") or "").partition("|")[0])
        except ValueError:
            continue
        cur = reach[ab]
        cur[sid] = max(cur.get(sid, 0), n)
    if not reach:
        return None
    rows = []
    for ab, per_sid in sorted(reach.items(), key=lambda kv: -len(kv[1])):
        vals = list(per_sid.values())
        avg = sum(vals) / len(vals)
        # Индекс шага 0 — это «прошёл ноль шагов», поэтому глубина считается от
        # числа ПЕРЕХОДОВ (total-1), иначе даже закрывший заставку выглядел бы
        # прошедшим 9% квиза.
        pct = avg / (total - 1) * 100 if total > 1 else 0
        done = sum(1 for v in vals if v >= total - 1)
        rows.append([ab, len(vals), f"{avg:.1f} из {total - 1}", f"{pct:.0f}%",
                     done, f"{done / len(vals) * 100:.0f}%"])
    return {"title": "Глубина прохождения квиза", "tag": "по лендингам",
            "table": {"head": ["Лендинг", "Начали", "Средний шаг", "Пройдено",
                               "Дошли до конца", "%"], "rows": rows}}


def _step_table(events: list[dict], steps: list[tuple[int, str]],
                payments: list[dict], ab: str, token: str) -> dict | None:
    """Шаг за шагом: сколько дошло и сколько потеряли ИМЕННО ЗДЕСЬ.

    Последние строки — оплата: без них таблица обрывается на пейволле, а самый
    дорогой отвал происходит уже после него.
    """
    if not steps:
        return None
    ev = [e for e in events if ab == "all" or (e.get("ab") or "") == ab]
    by_step: dict[int, set] = defaultdict(set)
    for e in ev:
        if e.get("name") != "quiz_step" or not e.get("sid"):
            continue
        try:
            by_step[int(str(e.get("part") or "").partition("|")[0])].add(e["sid"])
        except ValueError:
            continue
    if not by_step:
        return None
    started = len(by_step.get(steps[0][0], set())) or 1
    rows, prev = [], None
    for n, title in steps:
        cnt = len(by_step.get(n, set()))
        drop = "—" if prev is None else (f"−{(prev - cnt) / prev * 100:.0f}%" if prev else "—")
        rows.append([f"{n}. {title}", cnt, f"{cnt / started * 100:.0f}%", drop])
        prev = cnt
    # Оплата — теми же двумя колонками, что и шаги: доля от начавших квиз и
    # потеря на этом переходе. Иначе пейволл и касса выпадают из картины.
    for name, label in (("pay_click", "→ Нажали «Оформить»"),
                        ("yookassa_reached", "→ Дошли до ЮKassa")):
        cnt = len({e["sid"] for e in ev if e.get("name") == name and e.get("sid")})
        drop = f"−{(prev - cnt) / prev * 100:.0f}%" if prev else "—"
        rows.append([label, cnt, f"{cnt / started * 100:.0f}%", drop])
        prev = cnt
    paid = [p for p in payments if ab == "all" or (p.get("variant") or "") == ab]
    rows.append(["→ Оплатили", len(paid), f"{len(paid) / started * 100:.1f}%",
                 f"−{(prev - len(paid)) / prev * 100:.0f}%" if prev else "—"])
    labels = ["all"] + sorted({(e.get("ab") or "") for e in events if e.get("ab")})
    controls = [{"label": "Все лендинги" if x == "all" else x,
                 "href": f"?token={token}&ab={x}", "active": x == ab} for x in labels]
    return {"title": "Шаги квиза", "icon": "spark",
            "tag": ("все лендинги" if ab == "all" else f"лендинг: {ab}"),
            "controls": controls,
            "table": {"head": ["Шаг", "Дошло", "% от начавших", "Потеря на шаге"],
                      "rows": rows}}


def build(data_dir: str, payments: list[dict], counters: dict, since: str = "",
          ab: str = "all", token: str = "") -> list[dict]:
    """Все проектные панели. Пустые срезы не показываем: панель без строк —
    это не «нет данных», а шум, из-за которого не видно панелей с данными."""
    ev = _events(data_dir, since)
    panels = []
    # Шаги квиза — первыми: между «зашли» и «оплатили» у нас одиннадцать
    # вопросов, и именно там теряется большая часть людей.
    steps = _steps_of(ev)
    for p in (_step_table(ev, steps, payments, ab, token), _depth(ev, steps)):
        if p:
            panels.append(p)
    p = _tariffs(ev, payments)
    if any(r[1] or r[2] for r in p["table"]["rows"]):
        panels.append(p)
    for field, title, labels in (
            ("ab", "Лендинг (плечо A/B)", None),
            ("dev", "Устройство", None),
            ("os", "ОС", None),
            ("br", "Браузер", None),
            ("ref", "Откуда перешли", None)):
        s = _slice(ev, field, title, labels)
        if s:
            panels.append(s)
    # Здоровье выдачи — то, чего в модуле нет и быть не может: он про поведение,
    # а это про то, доехал ли товар. Деградировавший план оплачен, но не выдан.
    deg = int(counters.get("plan_degraded", 0) or 0)
    sold = int(counters.get("fulfill_degraded", 0) or 0)
    fixed = int(counters.get("plan_repaired", 0) or 0)
    fail = int(counters.get("fulfill_fail", 0) or 0)
    if deg or sold or fail:
        panels.append({
            "title": "Здоровье выдачи", "tag": "план собран или нет",
            "banner": ({"tone": "bad", "text": f"<b>{sold}</b> оплативших получили заготовку без рецептов"}
                       if sold else None),
            "kv": [["Планов ушло в заготовку", deg], ["Из них оплатившим", sold],
                   ["Дособрано кроном", fixed], ["Доставка упала совсем", fail]]})
    return panels
