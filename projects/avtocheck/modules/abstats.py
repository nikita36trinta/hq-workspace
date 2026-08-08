"""A/B sample-size tracker: счётчик назначений вариантов + сводка со значимостью.

КЛЮЧЕВОЙ инвариант — CVR считается по ОДНОМУ временному окну: и знаменатель
(назначения), и числитель (оплаты) берутся с момента старта счётчика (`since`).
Иначе исторические оплаты (за недели) делятся на назначения (за часы) и дают
бессмысленные 100% + ложную значимость. Оплаты ДО старта счётчика показываются
отдельной справкой и в CVR/значимость НЕ идут.

Счётчик живёт рядом с реестром оплат (тот же персистентный volume /app/data),
чтобы переживать деплой.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bump_assignment(ledger_dir: Path, variant: str, src: str = "organic",
                    campaign: str = "") -> None:
    """Инкремент счётчика назначений для варианта, РАЗДЕЛЬНО по источнику
    (src='ad'|'organic') И по кампании (by_camp, для A/B в разрезе кампаний).
    Формат: {"__since__":.., "ad":{A:..}, "organic":{A:..}, "by_camp":{"sdelka_hot":{A:..}}}."""
    src = "ad" if src == "ad" else "organic"
    try:
        f = Path(ledger_dir) / "ab_assign.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(f.read_text()) if f.exists() else {}
        # миграция старого плоского формата {A:n,...} → organic
        if data and "ad" not in data and "organic" not in data:
            since = data.pop("__since__", _now_iso())
            data = {"__since__": since, "ad": {}, "organic": dict(data)}
        data.setdefault("__since__", _now_iso())
        data.setdefault("ad", {}); data.setdefault("organic", {})
        data[src][variant] = int(data[src].get(variant, 0)) + 1
        if campaign:  # разбивка назначений по кампании
            bc = data.setdefault("by_camp", {})
            bc.setdefault(campaign, {})[variant] = int(bc.get(campaign, {}).get(variant, 0)) + 1
        f.write_text(json.dumps(data))
    except Exception:
        pass


def bump_counter(ledger_dir: Path, name: str) -> None:
    """Инкремент именованного счётчика воронки (напр. 'addon_interest' — клики
    по тизеру ИНН-апселла). Хранится в counters.json рядом с реестром."""
    try:
        f = Path(ledger_dir) / "counters.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(f.read_text()) if f.exists() else {}
        data[name] = int(data.get(name, 0)) + 1
        f.write_text(json.dumps(data))
    except Exception:
        pass


def read_counter(ledger_dir: Path, name: str) -> int:
    try:
        f = Path(ledger_dir) / "counters.json"
        if f.exists():
            return int(json.loads(f.read_text()).get(name, 0))
    except Exception:
        pass
    return 0


def set_counter(ledger_dir: Path, name: str, value: int) -> None:
    """Записать точное значение счётчика (напр. timestamp последнего алерта)."""
    try:
        f = Path(ledger_dir) / "counters.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(f.read_text()) if f.exists() else {}
        data[name] = int(value)
        f.write_text(json.dumps(data))
    except Exception:
        pass


def read_assignments(ledger_dir: Path, src: str = "all",
                     campaign: str | None = None) -> tuple[dict[str, int], str | None]:
    """Вернуть (счётчики назначений по вариантам, since). src: 'all'|'ad'|'organic'.
    campaign задан → назначения только этой кампании (из by_camp), src игнорируется."""
    try:
        f = Path(ledger_dir) / "ab_assign.json"
        if not f.exists():
            return {}, None
        raw = json.loads(f.read_text())
        since = raw.get("__since__")
        if campaign:
            bc = {k: int(v) for k, v in ((raw.get("by_camp") or {}).get(campaign) or {}).items()}
            return bc, since
        # старый плоский формат {A:n} трактуем как organic
        if "ad" not in raw and "organic" not in raw:
            flat = {k: int(v) for k, v in raw.items()
                    if k != "__since__" and str(v).lstrip("-").isdigit()}
            ad, org = {}, flat
        else:
            ad = {k: int(v) for k, v in (raw.get("ad") or {}).items()}
            org = {k: int(v) for k, v in (raw.get("organic") or {}).items()}
        if src == "ad":
            return ad, since
        if src == "organic":
            return org, since
        merged: dict[str, int] = {}
        for d in (ad, org):
            for k, v in d.items():
                merged[k] = merged.get(k, 0) + v
        return merged, since
    except Exception:
        pass
    return {}, None


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _two_prop_p(c1: int, n1: int, c2: int, n2: int) -> float | None:
    """Двусторонний z-тест разности двух долей. None, если данных нет."""
    if n1 <= 0 or n2 <= 0:
        return None
    p1, p2 = c1 / n1, c2 / n2
    pool = (c1 + c2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    z = (p1 - p2) / se
    return math.erfc(abs(z) / math.sqrt(2))  # two-sided p-value


def build_stats(variants: list[str], assignments: dict[str, int],
                since: str | None, payments: list[dict[str, Any]],
                src: str = "all", campaign: str | None = None) -> dict[str, Any]:
    since_dt = _parse_dt(since or "")
    # aligned — оплаты в окне счётчика (в CVR); hist_all — за всё время (справка).
    aligned_paid = {v: 0 for v in variants}
    aligned_rev = {v: 0 for v in variants}
    hist_all = {v: 0 for v in variants}       # все оплаты по варианту (инфо)
    other_paid = 0                            # оплаты без валидной метки варианта
    other_rev = 0
    tariffs: dict[str, dict[str, int]] = {}   # оплаты по тарифам (апселл и пр.)
    for r in payments:
        # Фильтр по источнику трафика (реклама/органика). '' в старых оплатах
        # трактуем как organic (до внедрения метки src).
        rsrc = "ad" if (r.get("src") == "ad") else "organic"
        if src in ("ad", "organic") and rsrc != src:
            continue
        if campaign and (r.get("campaign") or "") != campaign:  # разрез по кампании
            continue
        v = (r.get("variant") or "").strip()
        amount = int(r.get("amount") or 0)
        tk = (r.get("tariff") or "?").strip() or "?"
        pdt = _parse_dt(r.get("paid_at") or "")
        # Окно счётчика: продажи по тарифам считаем ТОЛЬКО в окне since (как воронка/деньги),
        # чтобы «сброс» (сдвиг since) обнулял и панель «Продажи по тарифам». hist_all ниже —
        # намеренно за всё время (справочная колонка A/B). Реальные платежи не удаляются.
        in_window = (since_dt is None) or (pdt is not None and pdt >= since_dt)
        if in_window:
            t = tariffs.setdefault(tk, {"count": 0, "revenue": 0})
            t["count"] += 1
            t["revenue"] += amount
        # В сравнение ПЛЕЧ (CVR/₽-визит/значимость) идёт ТОЛЬКО базовый тариф —
        # это цена, которую тестируем. ext/realtor/monitor/addon — отдельные
        # продукты, их выручка не относится к цене базового отчёта и исказила бы
        # ₽/визит плеча (напр. один realtor=9900₽ ≈ 14 базовых по 690₽).
        if tk != "base":
            continue
        if v not in aligned_paid:
            if in_window:
                other_paid += 1
                other_rev += amount
            continue
        hist_all[v] += 1
        if in_window:
            aligned_paid[v] += 1
            aligned_rev[v] += amount

    rows = []
    for v in variants:
        n = int(assignments.get(v, 0))
        c = aligned_paid[v]
        rows.append({
            "variant": v, "assigned": n, "paid": c,
            "cvr": (c / n) if n else 0.0, "revenue": aligned_rev[v],
            "hist_paid": hist_all[v],
        })
    leader = max((r for r in rows if r["assigned"] > 0),
                 key=lambda r: r["cvr"], default=None)
    for r in rows:
        if (leader and r["variant"] != leader["variant"]
                and r["assigned"] > 0 and leader["assigned"] > 0):
            p = _two_prop_p(leader["paid"], leader["assigned"], r["paid"], r["assigned"])
            r["p_vs_leader"] = p
            r["sig"] = (p is not None and p < 0.05)
        else:
            r["p_vs_leader"] = None
            r["sig"] = False

    aligned_total = sum(aligned_paid.values())
    min_paid_arm = min((r["paid"] for r in rows if r["assigned"] > 0), default=0)
    # Значимость/готовность имеют смысл только когда в окне есть оплаты.
    enough = (min_paid_arm >= 25) and aligned_total > 0
    # Апселл: доля берущих ИНН-допроверку от числа купивших базовый отчёт.
    base_cnt = tariffs.get("base", {}).get("count", 0)
    addon_cnt = tariffs.get("addon", {}).get("count", 0)
    addon_take = (addon_cnt / base_cnt) if base_cnt else 0.0
    total_rev = sum(t["revenue"] for t in tariffs.values())
    return {
        "rows": rows,
        "src": src,
        "leader": leader["variant"] if leader else None,
        "since": since,
        "total_assigned": sum(r["assigned"] for r in rows),
        "aligned_paid": aligned_total,
        "hist_paid_total": sum(hist_all.values()),
        "other_paid": other_paid,
        "other_revenue": other_rev,
        "min_paid_arm": min_paid_arm,
        "enough": enough,
        "tariffs": tariffs,
        "addon_take": addon_take,
        "addon_cnt": addon_cnt,
        "base_cnt": base_cnt,
        "total_revenue": total_rev,
    }


_TARIFF_LABELS = {
    "base": "Базовый отчёт", "addon": "ИНН: банкротство+арбитраж (апселл)",
    "ext": "Расширенная", "monitor": "Мониторинг 30 дней",
    "realtor": "Пакет риелторам", "?": "без тарифа",
}


def _fmt_dur(sec: int) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m}м {s:02d}с" if m else f"{s}с"


def _extras_html(stats: dict[str, Any], extras: dict[str, Any] | None) -> str:
    extras = extras or {}
    blocks = []

    # --- Баланс NewDB (поставщик данных проверок) ---
    if "newdb_balance" in extras:
        bal = extras["newdb_balance"]
        if bal is None:
            card = "<b>NewDB баланс:</b> <span style='color:#b00'>не удалось получить</span>"
        else:
            low = bal < 500
            col = "#b00" if bal < 200 else ("#c77700" if low else "#0a7d33")
            warn = " ⚠️ при нуле проверки ЕГРН/ФССП падают, отчёты выходят пустыми" if low else ""
            card = (f"<b>NewDB баланс:</b> <span style='color:{col};font-weight:700'>{bal} ₽</span>"
                    f"<span class=note>{warn}</span>")
        blocks.append(f"<div class=card>{card}</div>")

    # --- Вовлечённость (Метрика) ---
    eng = extras.get("engagement")
    if eng:
        lbl = extras.get("engagement_label", "сегодня")
        cells = [
            ("Визиты", str(eng.get("visits", 0))),
            ("Посетители", str(eng.get("users", 0))),
            ("Отказы", f"{eng.get('bounce_rate', 0)}%"),
            ("Ср. время на сайте", _fmt_dur(eng.get("avg_seconds", 0))),
            ("Глубина", f"{eng.get('page_depth', 0)} стр."),
        ]
        tiles = "".join(
            f"<div class=tile><div class=tv>{v}</div><div class=tk>{k}</div></div>"
            for k, v in cells)
        blocks.append(f"<div class=card><b>Вовлечённость ({lbl}, из Метрики)</b>"
                      f"<div class=tiles>{tiles}</div></div>")

    # --- Апселл / тарифы ---
    tariffs = stats.get("tariffs") or {}
    if tariffs:
        order = ["base", "addon", "ext", "monitor", "realtor", "?"]
        keys = [k for k in order if k in tariffs] + [k for k in tariffs if k not in order]
        trs = "".join(
            f"<tr><td>{_TARIFF_LABELS.get(k, k)}</td>"
            f"<td>{tariffs[k]['count']}</td><td>{tariffs[k]['revenue']} ₽</td></tr>"
            for k in keys)
        take = stats.get("addon_take", 0.0)
        take_line = (f"<b>ИНН-апселл берут {take*100:.1f}%</b> покупателей базового отчёта "
                     f"({stats.get('addon_cnt',0)} из {stats.get('base_cnt',0)}). "
                     f"Общая выручка по всем тарифам: <b>{stats.get('total_revenue',0)} ₽</b>.")
        # Воронка апселла: клик по тизеру → оплата (видно, режет ли цена).
        funnel = ""
        interest = extras.get("addon_interest")
        if interest is not None:
            addon_paid = stats.get("addon_cnt", 0)
            conv = (addon_paid / interest * 100) if interest else 0.0
            funnel = (f"<p class=note><b>Воронка апселла:</b> нажали «Проверить по ИНН» "
                      f"<b>{interest}</b> → оплатили <b>{addon_paid}</b> "
                      f"(конверсия модалки <b>{conv:.0f}%</b>). "
                      f"{'Много кликов, мало оплат — вероятно, цена высока.' if interest >= 5 and conv < 25 else ''}</p>")
        blocks.append(
            "<div class=card><b>Продажи по тарифам (за всё время)</b>"
            "<table class=mini><tr><th>Тариф</th><th>Оплат</th><th>Выручка</th></tr>"
            f"{trs}</table><p class=note>{take_line}</p>{funnel}</div>")

    return "".join(blocks)


def _funnel_html(fn: dict[str, Any] | None) -> str:
    """Визуальная воронка: бар на каждый шаг + % от визитов и % отвала от прошлого шага."""
    if not fn or not fn.get("steps"):
        return ""
    steps = fn["steps"]
    top = max((s["count"] for s in steps), default=0) or 1
    rows = []
    for s in steps:
        w = max(2, round(s["count"] / top * 100))
        pv = f"{s['pct_visit']*100:.0f}%"
        # % отвала от предыдущего шага (красным, если люди теряются)
        drop = ""
        if s["pct_prev"] is not None and s["pct_prev"] <= 1:
            lost = (1 - s["pct_prev"]) * 100
            col = "#b00" if lost >= 40 else ("#c67a00" if lost >= 20 else "#0a7d33")
            drop = f'<span style="color:{col};font-size:12.5px;margin-left:8px">↓ {lost:.0f}% ушло</span>'
        # для «Запустили проверку» — сплит адрес/кадастр
        extra = ""
        if s["name"] == "check_started" and fn.get("obj"):
            o = fn["obj"]
            extra = (f'<div class=note style="margin:2px 0 0 4px">'
                     f'адрес: <b>{o.get("address",0)}</b> · кадастр: <b>{o.get("cadastre",0)}</b>'
                     + (f' · иное: {o.get("other",0)}' if o.get("other") else '') + '</div>')
        rows.append(
            f'<div style="margin:9px 0"><div style="display:flex;align-items:baseline;gap:10px">'
            f'<span style="min-width:210px;font-weight:600">{s["label"]}</span>'
            f'<span style="min-width:44px;font-weight:700;font-size:17px">{s["count"]}</span>'
            f'<span class=note>{pv} от визитов</span>{drop}</div>'
            f'<div style="height:14px;background:#e8eef4;border-radius:5px;margin-top:3px;overflow:hidden">'
            f'<div style="height:100%;width:{w}%;background:linear-gradient(90deg,#2563eb,#3b82f6);border-radius:5px"></div></div>'
            f'{extra}</div>')
    # акцент: отвал на загрузке (главный вопрос — не слишком ли долго ждут)
    ld = ""
    if fn.get("loading_drop") is not None:
        d = fn["loading_drop"] * 100
        col = "#b00" if d >= 30 else ("#c67a00" if d >= 15 else "#0a7d33")
        verdict = ("многовато — стоит сократить время загрузки" if d >= 30
                   else "терпимо" if d >= 15 else "хорошо — почти все дожидаются")
        ld = (f'<div class="card" style="margin-top:10px;border-color:{col}">'
              f'<b>Отвал на загрузке:</b> из <b>{fn["loading_started"]}</b> запустивших проверку '
              f'дождались результата <b>{fn["loading_completed"]}</b> → '
              f'<b style="color:{col}">потеряли {d:.0f}%</b> во время ожидания ({verdict}).</div>')
    # распределение «на какой секунде ушли»
    ab = fn.get("abandon")
    ab_html = ""
    if ab:
        mx = max((c for _, c in ab["dist"]), default=0) or 1
        bars = ""
        for lbl, c in ab["dist"]:
            w = round(c / mx * 100)
            bars += (f'<div style="display:flex;align-items:center;gap:10px;margin:4px 0">'
                     f'<span style="min-width:60px;font-size:13px">{lbl}</span>'
                     f'<div style="flex:1;height:13px;background:#eef;border-radius:4px;overflow:hidden">'
                     f'<div style="height:100%;width:{w}%;background:#e0912b"></div></div>'
                     f'<span style="min-width:26px;text-align:right;font-weight:600">{c}</span></div>')
        ab_html = (
            f'<div class="card" style="margin-top:10px">'
            f'<b>На какой секунде уходят (во время загрузки):</b> '
            f'медиана <b>{ab["median"]}с</b> · среднее <b>{ab["avg"]}с</b> · '
            f'чаще всего в интервале <b>{ab["max_bucket"]}</b> (всего ушедших: {ab["n"]})'
            f'<div style="margin-top:8px">{bars}</div>'
            f'<div class=note style="margin-top:6px">Ориентир: если большинство уходит раньше, '
            f'чем таймер (сейчас 55–120с) — стоит сократить время загрузки примерно до медианы.</div></div>')
    # --- Группа 1: денежная петля ---
    money_html = ""
    m = fn.get("money")
    if m and (m["pay_click"] or m["paid"]):
        def dr(v):
            return "" if v is None else f' <span style="color:{"#b00" if v>=0.4 else "#c67a00" if v>=0.2 else "#0a7d33"};font-size:12.5px">↓ {v*100:.0f}%</span>'
        bump_txt = ("" if m["bump_rate"] is None else
                    f'<div style="margin-top:8px">Расширенный отчёт (+199₽) выбирают: '
                    f'<b>{m["bump_yes"]}/{m["bump_total"]}</b> = <b>{m["bump_rate"]*100:.0f}%</b> кликнувших оплату '
                    f'<span class=note>(рост среднего чека)</span></div>')
        money_html = (
            '<h2>Денежная петля (клик → ЮKassa → оплата)</h2><div class="card">'
            f'<div>Нажали «Оплатить»: <b>{m["pay_click"]}</b></div>'
            f'<div>Дошли до страницы ЮKassa: <b>{m["yookassa"]}</b>{dr(m["drop_click_yk"])}</div>'
            f'<div>Оплатили: <b>{m["paid"]}</b>{dr(m["drop_yk_paid"])}</div>'
            f'{bump_txt}'
            '<div class=note style="margin-top:6px">Большой отвал «ЮKassa → оплата» = проблема на самой странице оплаты '
            '(цена/способы/доверие). Отвал «клик → ЮKassa» = техническая ошибка создания платежа.</div>'
            '</div>')

    # --- Группа 5: объект найден → конверсия ---
    lift_html = ""
    lf = fn.get("lift")
    if lf and (lf["with"][0] or lf["without"][0]):
        w, wo = lf["with"], lf["without"]
        lift_html = (
            '<h2>Объект найден в ЕГРН → конверсия</h2><div class="card">'
            f'<div>Показали объект: <b>{w[0]}</b> сессий → кликнули оплату <b>{w[1]}</b> ({w[2]*100:.0f}%)</div>'
            f'<div>Без объекта: <b>{wo[0]}</b> сессий → кликнули оплату <b>{wo[1]}</b> ({wo[2]*100:.0f}%)</div>'
            '<div class=note style="margin-top:6px">Если «с объектом» конвертит заметно лучше — бесплатное превью объекта '
            '(НСПД/DaData) окупается, стоит усиливать этот блок.</div></div>')

    # --- Группа 5: трение формы ---
    fr_html = ""
    fr = fn.get("friction")
    if fr:
        items = "".join(f'<div>заполнили <b>{k}</b> и ушли: <b>{v}</b></div>' for k, v in fr)
        fr_html = ('<h2>Трение формы (ушли, не запустив проверку)</h2><div class="card">'
                   + items + '<div class=note style="margin-top:6px">Показывает, на каком поле бросают форму '
                   '(fio=ФИО, dob=дата, obj=объект, email=почта). Много «none» — уходят сразу, не начав.</div></div>')

    # --- Группа 4: LTV / повторные покупки ---
    ltv_html = ""
    lt = fn.get("ltv")
    if lt:
        rr = "" if lt["repeat_rate"] is None else f' = <b>{lt["repeat_rate"]*100:.0f}%</b>'
        ltv_html = (
            '<h2>Повторные покупки и средний чек</h2><div class="card">'
            f'<div>Уникальных покупателей: <b>{lt["buyers"]}</b> · всего оплат: <b>{lt["total_orders"]}</b></div>'
            f'<div>Купили повторно: <b>{lt["repeat_buyers"]}</b>{rr}</div>'
            + (f'<div>Средний чек: <b>{lt["aov"]} ₽</b></div>' if lt["aov"] else '')
            + '<div class=note style="margin-top:6px">Покупатель квартиры проверяет 3–5 вариантов — если повторных мало, '
            'стоит слать письмо «проверить ещё квартиру» (почти бесплатный доп-доход).</div></div>')

    return ('<h2>Воронка (с момента старта счётчика)</h2>'
            '<div class="card">' + "".join(rows) + '</div>' + ld + ab_html
            + money_html + ltv_html + lift_html + fr_html)


def render_html(title: str, stats: dict[str, Any],
                extras: dict[str, Any] | None = None) -> str:
    def pct(x: float) -> str:
        return f"{x * 100:.2f}%"

    extras_html = _extras_html(stats, extras)
    # Переключатель источника трафика (реклама / органика / всё).
    cur = stats.get("src", "all")
    tok = (extras or {}).get("token", "")
    split = (extras or {}).get("traffic_split") or {}
    def _sw(key, label):
        on = "background:#1a2b23;color:#fff" if cur == key else "background:#fff;color:#1a2b23"
        return (f'<a href="?token={tok}&src={key}" style="{on};border:1px solid #cbd5e1;'
                f'border-radius:8px;padding:7px 14px;text-decoration:none;font-size:13.5px;font-weight:600">{label}</a>')
    switcher = (
        '<div class="card" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        '<b style="margin-right:6px">Источник трафика:</b>'
        + _sw("all", f'Всё ({split.get("ad",0)+split.get("organic",0)})')
        + _sw("ad", f'🟢 Реклама ({split.get("ad",0)})')
        + _sw("organic", f'🌿 Органика ({split.get("organic",0)})')
        + '<span class=note style="margin-left:auto">назначений-визитов по источнику (реклама = Директ по yclid/utm)</span></div>'
    )
    src_label = {"all": "всё", "ad": "реклама", "organic": "органика"}.get(cur, "всё")
    funnel_html = _funnel_html((extras or {}).get("funnel"))
    aligned_total = stats["aligned_paid"]
    trs = []
    for r in stats["rows"]:
        lead = " 👑" if r["variant"] == stats["leader"] else ""
        # CVR/значимость показываем только когда в окне счётчика есть оплаты.
        if aligned_total == 0:
            cvr_cell, pcol = "—", "—"
        else:
            cvr_cell = pct(r["cvr"])
            p = r["p_vs_leader"]
            if p is None:
                pcol = "—"
            elif r["sig"]:
                pcol = f"<b style='color:#0a7d33'>{p:.3f} ✓ знач.</b>"
            else:
                pcol = f"{p:.3f}"
        rpv_cell = ("—" if (aligned_total == 0 or not r["assigned"])
                    else f"<b>{r['revenue'] / r['assigned']:.2f} ₽</b>")
        trs.append(
            f"<tr><td><b>{r['variant']}{lead}</b></td><td>{r['assigned']}</td>"
            f"<td>{r['paid']}</td><td>{cvr_cell}</td>"
            f"<td>{r['revenue']} ₽</td><td>{rpv_cell}</td><td>{pcol}</td>"
            f"<td class=hist>{r['hist_paid']}</td></tr>")

    since_txt = (stats["since"] or "—")[:19].replace("T", " ")
    if aligned_total == 0:
        state = ("<p style='background:#fff4e5;border:1px solid #f0c98a;padding:11px 15px;border-radius:6px'>"
                 "⏳ <b>Идёт накопление выровненной выборки.</b> CVR и значимость считаются только по оплатам, "
                 f"пришедшим ПОСЛЕ старта счётчика (с {since_txt} UTC), — их пока <b>0</b>, поэтому CVR/значимость "
                 "скрыты (иначе исторические оплаты делились бы на свежие визиты и давали ложные 100%). "
                 "Пока смотрите на столбец «оплат всего» и на накопление «назначено».</p>")
    elif stats["enough"]:
        state = "<p><b>Готовность:</b> <span style='color:#0a7d33'>данных достаточно для направленного вывода</span>.</p>"
    else:
        state = (f"<p><b>Готовность:</b> <span style='color:#b00'>данных мало</span> — нужно ≥25 оплат "
                 f"на плечо в окне счётчика (минимум сейчас {stats['min_paid_arm']}).</p>")
    other = ""
    if stats.get("other_paid"):
        other = (f"<p class=note>Ещё <b>{stats['other_paid']}</b> оплат "
                 f"({stats['other_revenue']} ₽) вообще без метки варианта (до старта трекинга) — "
                 f"тоже не в CVR.</p>")
    return f"""<!doctype html><meta charset=utf-8><title>{title}</title>
<style>body{{font:15px/1.55 system-ui,-apple-system,sans-serif;margin:40px;max-width:840px;color:#1a1a1a}}
table{{border-collapse:collapse;width:100%;margin:18px 0}}
th,td{{border:1px solid #e0e0e0;padding:9px 13px;text-align:left}}
th{{background:#f6f6f6}} h1{{font-size:22px}} h2{{font-size:17px;margin:26px 0 4px}}
.note{{color:#666;font-size:13px}} td.hist,th.hist{{color:#888;background:#fafafa}}
.card{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;margin:12px 0}}
.tiles{{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}}
.tile{{flex:1;min-width:120px;background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:10px 12px;text-align:center}}
.tv{{font-size:22px;font-weight:700}} .tk{{font-size:12px;color:#666}}
table.mini{{margin:8px 0}} table.mini td,table.mini th{{padding:6px 10px}}</style>
<h1>{title}</h1>
{extras_html}
{switcher}
<h2>A/B-тест <span style="font-size:14px;color:#666;font-weight:400">· источник: {src_label}</span></h2>
<p>Назначений (визитов-уников с {since_txt} UTC): <b>{stats['total_assigned']}</b> ·
оплат в окне: <b>{aligned_total}</b> · оплат за всё время: <b>{stats['hist_paid_total']}</b></p>
{state}
<table><tr><th>Вариант</th><th>Назначено</th><th>Оплат (в окне)</th><th>CVR</th><th>Выручка (в окне)</th><th>₽/визит</th><th>p vs лидер</th><th class=hist>оплат всего</th></tr>
{''.join(trs)}</table>
{other}
{funnel_html}
<p class=note>CVR = оплаты ÷ назначения в ОДНОМ окне (с момента старта счётчика). «оплат всего» (серая колонка) — за всю историю, для справки, в CVR не идёт.
Значимость — двусторонний z-тест доли против текущего лидера (👑), ✓ при p&lt;0.05. «Назначено» инкрементируется сервером при выдаче куки варианта (боты считаются тоже, одинаково по плечам).
Порог ≥25 оплат/плечо — для направленного чтения; строгая проверка удвоения CVR требует ~2300 визитов/плечо.</p>"""


# ---------- Воронка событий (funnel) ----------

# Порядок шагов воронки + человекочитаемые метки.
# Строго убывающая последовательность (каждый шаг — подмножество предыдущего в реальном потоке).
# email_left — шумный сигнал (blur поля), в основную воронку не берём (логируется, но не рисуется).
FUNNEL_STEPS = [
    ("visit", "Зашли на сайт"),
    ("checkout_open", "Открыли форму проверки"),
    ("check_started", "Запустили проверку"),
    ("check_completed", "Дождались результата"),
    ("pay_click", "Нажали «Оплатить»"),
]


def log_goal(ledger_dir: Path, sid: str, name: str, obj: str = "",
             sec: int | None = None, extra: dict | None = None) -> None:
    """Записать событие воронки. sid — идентификатор сессии (кука), для подсчёта уников.
    sec — секунда ухода (check_abandoned). extra — доп-поля (bump/shown/fields)."""
    try:
        f = Path(ledger_dir) / "funnel.jsonl"
        from datetime import datetime, timezone
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "sid": sid, "name": name}
        if obj:
            rec["obj"] = obj
        if sec is not None:
            rec["sec"] = sec
        if extra:
            rec.update(extra)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def build_funnel(ledger_dir: Path, since: str | None, payments: list | None = None) -> dict[str, Any]:
    """Собрать воронку + денежную петлю, bump, объект→конверсия, трение, повторные покупки."""
    f = Path(ledger_dir) / "funnel.jsonl"
    seen: dict[str, set] = {k: set() for k, _ in FUNNEL_STEPS}
    obj_split = {"address": set(), "cadastre": set(), "other": set()}
    abandon_secs: list[int] = []          # секунды ухода на загрузке
    pay_click_sids: set = set()           # сессии, кликнувшие «Оплатить»
    yk_sids: set = set()                  # дошли до ЮKassa
    bump_yes = 0                          # выбрали расширенный (pay_click bump=1)
    bump_total = 0
    obj_shown: dict[str, int] = {}        # sid → показан ли объект (для лифта)
    friction: dict[str, int] = {}         # набор заполненных полей при уходе с формы
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if since and (r.get("ts") or "") < since:
                continue
            nm, sid = r.get("name"), r.get("sid") or ""
            if nm in seen and sid:
                seen[nm].add(sid)
            if nm == "check_started" and sid:
                ot = r.get("obj") or "other"
                obj_split.setdefault(ot, set()).add(sid)
            if nm == "check_abandoned" and isinstance(r.get("sec"), int):
                abandon_secs.append(r["sec"])
            if nm == "pay_click" and sid:
                pay_click_sids.add(sid)
                bump_total += 1
                if r.get("bump"):
                    bump_yes += 1
            if nm == "yookassa_reached" and sid:
                yk_sids.add(sid)
            if nm == "preview_object" and sid:
                obj_shown[sid] = 1 if r.get("shown") else 0
            if nm == "checkout_abandon":
                key = r.get("fields") or "none"
                friction[key] = friction.get(key, 0) + 1
    visits = len(seen["visit"]) or 1
    steps = []
    prev = None
    for k, label in FUNNEL_STEPS:
        n = len(seen[k])
        steps.append({
            "name": k, "label": label, "count": n,
            "pct_visit": n / visits,
            "pct_prev": (n / prev) if (prev and prev > 0) else None,
        })
        prev = n
    started = len(seen["check_started"]) or 0
    completed = len(seen["check_completed"]) or 0
    # статистика ухода на загрузке: медиана/среднее секунды + распределение по корзинам
    abandon = None
    if abandon_secs:
        ss = sorted(abandon_secs)
        med = ss[len(ss) // 2]
        avg = sum(ss) / len(ss)
        buckets = [("0–15с", 0, 15), ("15–30с", 15, 30), ("30–45с", 30, 45),
                   ("45–60с", 45, 60), ("60–90с", 60, 90), ("90с+", 90, 10 ** 9)]
        dist = [(lbl, sum(1 for x in ss if lo <= x < hi)) for lbl, lo, hi in buckets]
        abandon = {"n": len(ss), "median": med, "avg": round(avg), "max_bucket": max(dist, key=lambda b: b[1])[0], "dist": dist}

    # --- Группа 1: денежная петля (клик → ЮKassa → оплата) ---
    paid_since = []
    for p in (payments or []):
        ts = p.get("paid_at") or p.get("ts") or ""
        if p.get("status") == "succeeded" and (not since or ts >= since):
            paid_since.append(p)
    pc, yk, pd = len(pay_click_sids), len(yk_sids), len(paid_since)
    money = {
        "pay_click": pc, "yookassa": yk, "paid": pd,
        "drop_click_yk": (1 - yk / pc) if pc else None,
        "drop_yk_paid": (1 - pd / yk) if yk else None,
        "bump_yes": bump_yes, "bump_total": bump_total,
        "bump_rate": (bump_yes / bump_total) if bump_total else None,
    }

    # --- Группа 5: объект найден → конверсия (лифт) ---
    lift = None
    if obj_shown:
        with_obj = [s for s, v in obj_shown.items() if v]
        without = [s for s, v in obj_shown.items() if not v]
        def conv(sids):
            n = len(sids)
            c = sum(1 for s in sids if s in pay_click_sids)
            return (n, c, (c / n) if n else 0.0)
        lift = {"with": conv(with_obj), "without": conv(without)}

    # --- Группа 5: трение формы (какие поля заполнили при уходе) ---
    friction_top = sorted(friction.items(), key=lambda kv: -kv[1])[:6] if friction else []

    # --- Группа 4: повторные покупки + средний чек ---
    ltv = None
    if paid_since:
        by_email: dict[str, int] = {}
        amounts = []
        for p in paid_since:
            em = (p.get("email") or "").strip().lower()
            if em:
                by_email[em] = by_email.get(em, 0) + 1
            try:
                amounts.append(float(p.get("amount") or 0))
            except Exception:
                pass
        repeat = sum(1 for v in by_email.values() if v > 1)
        ltv = {
            "buyers": len(by_email), "repeat_buyers": repeat,
            "repeat_rate": (repeat / len(by_email)) if by_email else None,
            "aov": round(sum(amounts) / len(amounts)) if amounts else None,
            "total_orders": len(paid_since),
        }

    return {
        "steps": steps,
        "obj": {k: len(v) for k, v in obj_split.items()},
        "loading_drop": (1 - completed / started) if started else None,
        "loading_started": started, "loading_completed": completed,
        "abandon": abandon,
        "money": money, "lift": lift, "friction": friction_top, "ltv": ltv,
    }
