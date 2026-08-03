"""Статистика NutriPlan: воронка, деньги и разрез по лендингам.

Зачем отдельный модуль
----------------------
Раньше /admin/stats отдавала сырой JSON из трёх ключей: визиты, лиды и два
служебных блока. На главный вопрос — «окупается ли реклама» — она не отвечала
вовсе: ни одной цифры выручки, ни одной даты. Всё считалось накопительным
итогом с самого запуска, то есть «как прошла неделя» узнать было нельзя.

Откуда берутся числа
--------------------
  counters.json — визиты, шаги квиза, лиды, оплаты. Кроме общего итога каждый
                  такой счётчик пишется ещё и посуточно («visit_slim@2026-08-01»,
                  см. _DAILY_PREFIXES в app.py), поэтому период считается
                  суммированием дней.
  orders.jsonl  — деньги. Строка СОЗДАНИЯ заказа несёт сумму, лендинг и метки,
                  строка payment.succeeded — факт оплаты и время. Соединяем их
                  по номеру заказа за один проход: суммы в строке оплаты нет.

Границы дня — московские: владелец смотрит на рекламу в своём часовом поясе, и
UTC сдвинул бы «вчера» на три часа.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3))
PERIODS = (("today", "Сегодня"), ("yesterday", "Вчера"), ("7d", "7 дней"),
           ("30d", "30 дней"), ("all", "Всё время"))


def _e(s) -> str:
    return html.escape(str(s), quote=True)


def _days_of(period: str) -> list[str] | None:
    """Список дней периода в виде «ГГГГ-ММ-ДД» по МСК. None — «всё время»."""
    today = datetime.now(MSK).date()
    if period == "today":
        return [today.isoformat()]
    if period == "yesterday":
        return [(today - timedelta(days=1)).isoformat()]
    if period == "7d":
        return [(today - timedelta(days=i)).isoformat() for i in range(7)]
    if period == "30d":
        return [(today - timedelta(days=i)).isoformat() for i in range(30)]
    return None


def _sum(counters: dict, name: str, days: list[str] | None) -> int:
    """Значение счётчика за период. Без периода — общий итог."""
    if days is None:
        try:
            return int(counters.get(name, 0) or 0)
        except (TypeError, ValueError):
            return 0
    total = 0
    for d in days:
        try:
            total += int(counters.get(f"{name}@{d}", 0) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _in_period(ts: str, days: list[str] | None) -> bool:
    if days is None:
        return True
    try:
        return datetime.fromisoformat(ts).astimezone(MSK).date().isoformat() in days
    except Exception:  # noqa: BLE001
        return False


def collect(counters: dict, orders_path: Path, landings: dict, period: str = "7d") -> dict:
    days = _days_of(period)

    # ── деньги: один проход по журналу заказов ────────────────────────────
    created: dict[str, dict] = {}      # заказ → строка создания (сумма, лендинг, метки)
    paid: list[dict] = []
    refunds: list[dict] = []
    if orders_path.exists():
        for line in orders_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue          # битую строку пропускаем, остальные считаем
            oid = r.get("order") or ""
            ev = r.get("event") or ""
            if not oid:
                continue
            if ev == "payment.succeeded":
                paid.append(r)
            elif ev == "refund.succeeded":
                refunds.append(r)
            elif r.get("amount") is not None:
                created.setdefault(oid, r)

    def _amount(rec: dict) -> float:
        # Сумма теперь пишется в саму строку оплаты. Фолбэк на строку создания —
        # для записей, сделанных до этого: у них суммы в строке оплаты нет.
        # У ПРОДЛЕНИЙ строки создания не существует в принципе, поэтому без
        # первого источника они давали ноль и выпадали из выручки целиком.
        for src in (rec, created.get(rec.get("order") or "") or {}):
            raw = src.get("amount")
            if raw not in (None, ""):
                try:
                    return float(str(raw).replace(",", "."))
                except ValueError:
                    pass
        return 0.0

    def _landing(rec: dict) -> str:
        return (rec.get("landing") or (created.get(rec.get("order") or "") or {}).get("landing")
                or "?")

    paid_p = [r for r in paid if _in_period(r.get("ts") or "", days)]
    ref_p = [r for r in refunds if _in_period(r.get("ts") or "", days)]
    revenue = sum(_amount(r) for r in paid_p)
    refunded = sum(abs(float(str(r.get("amount") or 0).replace(",", ".") or 0)) for r in ref_p)

    # ── воронка ───────────────────────────────────────────────────────────
    def _tot(prefix: str) -> int:
        return sum(_sum(counters, f"{prefix}{s}", days) for s in landings)

    visits, quizzes, leads = _tot("visit_"), _tot("quiz_"), _tot("lead_")
    pay_init = _tot("pay_init_") + _tot("sub_init_")
    # Типы разводим ЯВНО. Раньше «разовые» считались как «всё, что не подписка»,
    # и каждое продление попадало в них: на трёх оплатах дашборд показывал 3
    # разовых при одной настоящей. Продления берём из журнала, а не из счётчика:
    # счётчик пишется с суффиксом лендинга, а у продления лендинга не было.
    pays_all = len(paid_p)
    subs = sum(1 for r in paid_p if (r.get("type") or "") == "subscription")
    renews = sum(1 for r in paid_p if (r.get("type") or "") == "sub_renew")
    once = pays_all - subs - renews
    # Отмены суммируем по ВСЕМ ключам счётчика, включая «sub_cancel_?»: перебор
    # только по белому списку лендингов терял отмены без лендинга (на проде
    # показывалось 2 при трёх фактических).
    cancels = sum(_sum(counters, k.split("@")[0], days)
                  for k in {c.split("@")[0] for c in counters if c.startswith("sub_cancel_")})

    # ── по лендингам ──────────────────────────────────────────────────────
    # Строки строим по лендингам ПЛЮС по всем, что реально встретились в оплатах.
    # Цикл только по белому списку молча прятал деньги: продления приходили с
    # landing="?" и не попадали ни в одну строку — 43% выручки не было видно
    # нигде, хотя в плитке «Выручка» они учитывались. Две цифры на одном экране
    # расходились, и понять, какая правильная, было нельзя.
    seen = {_landing(r) for r in paid_p}
    rows = []
    for slug in list(landings) + sorted(s for s in seen if s not in landings):
        title = landings.get(slug, "Прочее / без лендинга").partition(" — ")[0]
        lp = [r for r in paid_p if _landing(r) == slug]
        rows.append({
            "slug": slug, "title": title,
            "visits": _sum(counters, f"visit_{slug}", days),
            "split": _sum(counters, f"split_{slug}", days),
            "quiz": _sum(counters, f"quiz_{slug}", days),
            "leads": _sum(counters, f"lead_{slug}", days),
            "ad": _sum(counters, f"visit_{slug}_ad", days),
            "pays": len(lp),
            "revenue": sum(_amount(r) for r in lp),
        })
    rows.sort(key=lambda r: (-r["revenue"], -r["visits"]))

    return {
        "period": period, "days": days,
        "funnel": [("Визиты", visits), ("Начали квиз", quizzes), ("Оставили почту", leads),
                   # В воронке — только НОВЫЕ покупки: продление не проходит ни
                   # визит, ни квиз, и в знаменателе шага «открыли оплату» его
                   # тоже нет. Считая его тут, мы получали конверсию 100% при
                   # фактических 60% — то есть завышали в 1,67 раза.
                   ("Открыли оплату", pay_init), ("Оплатили", subs + once)],
        "money": {"revenue": revenue, "refunded": refunded, "net": revenue - refunded,
                  "pays": pays_all, "subs": subs, "once": once,
                  "renews": renews, "cancels": cancels,
                  # Средний чек — по НОВЫМ покупкам: продления по своей природе
                  # равны цене подписки и, попадая в знаменатель, размывают
                  # ответ на вопрос «сколько платит новый покупатель».
                  "avg": (sum(_amount(r) for r in paid_p
                              if (r.get("type") or "") != "sub_renew") / (subs + once))
                         if (subs + once) else 0.0},
        "rows": rows,
        # Здесь же ошибки СОЗДАНИЯ платежа и записи на диск: без них неделя
        # лежачей кассы выглядела как «спроса нет» при зелёном «ошибок нет».
        "health": {k: int(counters.get(k, 0) or 0) for k in (
            "pay_create_error", "sub_create_error", "webhook_verify_fail", "webhook_error",
            "fulfill_fail", "fulfill_degraded", "fulfill_rescued", "mail_fail",
            "write_fail_lead", "write_fail_order", "write_fail_plan", "plan_degraded",
            "ip_limit_skipped", "lead_rate_limited", "pay_rate_limited", "forgot_global_capped")},
        # Шум снаружи — НЕ в «Здоровье». Ручка вебхука публичная, постучать в неё
        # может кто угодно, и любое ненулевое значение в блоке здоровья читается
        # как «у нас сломалось». Держим отдельно и подписываем словами.
        "noise": {k: int(counters.get(k, 0) or 0) for k in ("webhook_junk",)},
        # Возвраты вычитаем и здесь: строка в шапке — та, которую переносят в
        # отчёт, и «1 оплата на 299 ₽» при полном возврате означала бы деньги,
        # которых нет.
        "totals_all_time": {"pays": len(paid),
                            "revenue": sum(_amount(r) for r in paid)
                            - sum(abs(float(str(r.get("amount") or 0).replace(",", ".") or 0))
                                  for r in refunds)},
    }


# ── отрисовка ─────────────────────────────────────────────────────────────
_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:#F7F2E8;color:#1B2A17;padding:22px}
.wrap{max-width:1000px;margin:0 auto}
h1{font-size:24px;letter-spacing:-.02em}
.sub{color:#8B9584;font-size:13.5px;margin-top:4px}
.segs{display:flex;gap:7px;margin:18px 0;flex-wrap:wrap}
.seg{padding:8px 14px;border-radius:99px;background:#fff;border:1px solid #E6DECD;
  color:#4C5C46;text-decoration:none;font-weight:600;font-size:13.5px}
.seg.on{background:#1E9150;border-color:#1E9150;color:#fff}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:22px}
.tile{background:#fff;border:1px solid #E6DECD;border-radius:16px;padding:14px 16px}
.tile .cap{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:#8B9584}
.tile b{display:block;font-size:26px;letter-spacing:-.03em;margin-top:6px}
.tile b small{font-size:13px;color:#8B9584;font-weight:600}
.tile .note{font-size:12px;color:#8B9584;margin-top:4px}
.good b{color:#136B39}.bad b{color:#B45309}
h2{font-size:16px;margin:24px 0 10px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #E6DECD;border-radius:14px;overflow:hidden}
th,td{padding:9px 12px;text-align:right;font-variant-numeric:tabular-nums;border-top:1px solid #F0EADF}
th{background:#FBF8F1;font-size:12px;color:#6B7566;text-transform:uppercase;letter-spacing:.05em;border-top:0}
th:first-child,td:first-child{text-align:left}
tr:hover td{background:#FBFAF6}
.fun{background:#fff;border:1px solid #E6DECD;border-radius:14px;padding:6px 16px 14px}
.frow{display:flex;align-items:center;gap:12px;padding:9px 0;border-top:1px solid #F0EADF}
.frow:first-child{border-top:0}
.fname{width:150px;font-size:13.5px;color:#4C5C46}
.fbar{flex:1;height:10px;background:#F0EADF;border-radius:99px;overflow:hidden}
.fbar i{display:block;height:100%;background:#1E9150;border-radius:99px}
.fnum{width:70px;text-align:right;font-weight:700;font-variant-numeric:tabular-nums}
.fpct{width:64px;text-align:right;color:#8B9584;font-size:12.5px}
.warn{background:#FEF6E7;border-color:#E8D9B5}
.empty{color:#8B9584;padding:14px 2px;font-size:14px}
"""


def _n(x) -> str:
    """Разряды неразрывными пробелами: 12 345."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "0"
    s = f"{v:,.0f}".replace(",", " ")
    return s


def render(d: dict, token: str) -> str:
    p = d["period"]
    segs = "".join(
        f"<a class='seg{' on' if k == p else ''}' href='?token={_e(token)}&period={k}'>{_e(t)}</a>"
        for k, t in PERIODS)

    m = d["money"]
    tiles = [
        ("Выручка", f"{_n(m['revenue'])}<small> ₽</small>",
         f"возвраты {_n(m['refunded'])} ₽" if m["refunded"] else "возвратов нет",
         "good" if m["revenue"] else ""),
        ("Оплат", _n(m["pays"]),
         f"новых {m['subs'] + m['once']} · продлений {m['renews']}", ""),
        ("Средний чек", f"{_n(m['avg'])}<small> ₽</small>", "по новым покупкам", ""),
        ("Продлений", _n(m["renews"]), f"отмен {m['cancels']}",
         "bad" if m["cancels"] > m["renews"] and m["cancels"] else ""),
    ]
    tiles_html = "".join(
        f"<div class='tile {cls}'><span class='cap'>{_e(cap)}</span><b>{val}</b>"
        f"<div class='note'>{_e(note)}</div></div>" for cap, val, note, cls in tiles)

    # воронка: доля считается от ПРЕДЫДУЩЕГО шага — так видно, где именно теряем
    fun = d["funnel"]
    top = fun[0][1] or 1
    frows = ""
    prev = None
    for name, val in fun:
        # Доля от предыдущего шага. Больше 100% означает, что шаги в этих данных
        # НЕ вложены: счётчики независимы, и на старых днях «открыл оплату» мог
        # сработать без записанного лида (лимиты, notrack, заходы по прямой
        # ссылке). Рисовать «700%» — выдавать поломку за факт; показываем прочерк,
        # число при этом остаётся на месте.
        pct = (val / prev * 100) if prev else None
        show = "" if pct is None else ("—" if pct > 100.5 else f"{pct:.1f}%")
        frows += (f"<div class='frow'><span class='fname'>{_e(name)}</span>"
                  f"<span class='fbar'><i style='width:{min(100, val / top * 100):.1f}%'></i></span>"
                  f"<span class='fnum'>{_n(val)}</span>"
                  f"<span class='fpct'>{show}</span></div>")
        prev = val or None

    rows = d["rows"]
    if any(r["visits"] or r["pays"] for r in rows):
        trs = "".join(
            f"<tr><td>{_e(r['title'])}<br><span style='color:#8B9584;font-size:12px'>{_e(r['slug'])}</span></td>"
            f"<td>{_n(r['visits'])}</td><td>{_n(r['ad'])}</td><td>{_n(r['split'])}</td>"
            f"<td>{_n(r['quiz'])}</td><td>{_n(r['leads'])}</td><td>{_n(r['pays'])}</td>"
            f"<td><b>{_n(r['revenue'])}</b></td></tr>" for r in rows)
        # Колонка «Реклама» — то, ради чего дашборд и открывают: без неё нельзя
        # отличить платный трафик от органики, и вопрос «окупилась ли реклама»
        # оставался без ответа прямо на странице про рекламу.
        table = ("<table><tr><th>Лендинг</th><th>Визиты</th><th>Реклама</th><th>Раздано</th>"
                 f"<th>Квиз</th><th>Лиды</th><th>Оплат</th><th>Выручка, ₽</th></tr>{trs}</table>")
    else:
        table = "<div class='empty'>За этот период заходов не было.</div>"

    h = d["health"]
    bad = {k: v for k, v in h.items() if v}
    health = ("<div class='empty'>Ошибок нет.</div>" if not bad else
              "<table>" + "".join(f"<tr><td>{_e(k)}</td><td><b>{_n(v)}</b></td></tr>"
                                  for k, v in sorted(bad.items(), key=lambda x: -x[1])) + "</table>")
    junk = (d.get("noise") or {}).get("webhook_junk", 0)
    if junk:
        health += (f"<div class='empty'>Ещё {_n(junk)} — уведомления о платежах, которых у "
                   f"ЮKassa нет: кто-то стучится в публичную ручку. Это не наша поломка.</div>")

    ta = d["totals_all_time"]
    label = dict(PERIODS).get(p, p)
    # Честная оговорка: посуточные счётчики появились с выкладкой, и за более
    # ранние дни их просто нет. Без этой строки пустой «30 дней» читался бы как
    # «продаж не было», а не «данных не было».
    note = ("Посуточная статистика ведётся с 1 августа 2026 — за более ранние дни "
            "цифры показывает только «Всё время». ")
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'>"
        f"<title>Статистика · NutriPlan</title><style>{_CSS}</style></head><body><div class='wrap'>"
        f"<h1>NutriPlan · {_e(label)}</h1>"
        f"<div class='sub'>{_e(note)}Всего с запуска: {_n(ta['pays'])} оплат "
        f"на {_n(ta['revenue'])} ₽.</div>"
        f"<div class='segs'>{segs}</div>"
        f"<div class='tiles'>{tiles_html}</div>"
        f"<h2>Воронка</h2><div class='fun'>{frows}</div>"
        f"<h2>Лендинги</h2>{table}"
        f"<h2>Здоровье</h2>{health}"
        "</div></body></html>")
