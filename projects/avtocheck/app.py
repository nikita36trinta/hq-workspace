"""
ЧистаяСделка — MVP. FastAPI бэкенд + вшитый лендинг.

Флоу:
  GET  /                        — лендинг (static/index.html)
  POST /api/check               — запуск проверки продавца+объекта; возвращает
                                  уровень риска, найденные флаги, ссылку на PDF
  GET  /api/report/{rid}        — скачать PDF-отчёт
  POST /api/pay                 — создать заказ + инициировать платёж в ЮKassa
                                  (в bypass-режиме — честно помеченный демо-шаг)
  GET  /pay/return              — возврат из ЮKassa: заново проверяет статус
                                  через GET /v3/payments/{id} и выдаёт PDF
  POST /api/yookassa/webhook    — уведомления ЮKassa; статус подтверждается
                                  повторным вызовом API, идемпотентно
  GET  /api/tariffs             — список тарифов
"""
from __future__ import annotations

import json
import os
import threading
import re
import uuid

# Паспорт машины и периоды владения из последней сборки — ПО ПОТОКУ.
# Общая переменная здесь означала бы, что два одновременных платежа
# перезапишут данные друг друга (см. _run_checks).
_auto_extra = threading.local()
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from modules.efrsb import check_bankruptcy
from modules.fssp import check_enforcement
from modules.courts import check_courts
from modules.verdict import Check, Verdict, build_verdict
from modules.pdf import render_report
from modules.payment import (
    TARIFFS,
    YooKassaError,
    create_payment,
    fetch_payment,
    is_configured as payments_configured,
)
from modules import orders
from modules import abstats

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
REPORTS_DIR = ROOT / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# in-memory store — MVP-уровня; в проде заменить на БД
REPORTS: dict[str, dict[str, Any]] = {}

app = FastAPI(title="ЧистаяСделка API", version="0.1.0")


# ---------- Переиспользуемый пайплайн аналитики (вендор-модуль) ----------
def _an_payments() -> list[dict[str, Any]]:
    """Платежи в формате модуля аналитики (для денег/LTV/NET по варианту)."""
    out = []
    for p in orders.read_payments():
        if p.get("test"):
            continue        # свои проверочные оплаты — мимо выручки, LTV и NET
        out.append({
            "status": p.get("status"), "amount": p.get("amount"),
            "email": p.get("email"), "ts": p.get("paid_at") or p.get("ts"),
            "variant": p.get("variant"), "order": p.get("order_id"),
            "method": p.get("payment_method") or p.get("method"),
            "refunded": p.get("refunded"), "chargeback": p.get("chargeback"),
            "src": p.get("src"), "campaign": p.get("campaign"),
        })
    return out


# Точка отсчёта периода «С начала теста». Раньше бралась из счётчика A/B-назначений
# (24.07), но 05.08 сменились цены, развилки и апселлы — данные до этой даты про
# другой продукт. Меняя цены снова, сдвигать и эту метку.
PRODUCT_SINCE = "2026-08-04T21:00:00"     # 05.08.2026 00:00 МСК


def _an_since():
    return PRODUCT_SINCE


def _an_extra_kpis(ctx: dict) -> list[dict]:
    """Доп-плитки в верхнюю сводку: баланс поставщика данных (ops-сигнал).

    Поставщик здесь apipoint, а не NewDB: NewDB — ЕГРН и ФССП, к автомобилям
    отношения не имеет, и его баланс на этом дашборде вводил в заблуждение.

    Отдельного метода «покажи баланс» у apipoint нет, зато остаток приходит в
    КАЖДОМ ответе — мы пишем его в журнал вызовов. Берём последнюю запись и
    честно говорим, на какой момент число верно: спрашивать баланс отдельным
    платным вызовом ради плитки было бы глупо.
    """
    out = []
    try:
        import json as _json
        from datetime import datetime, timezone
        path = Path(os.getenv("APIPOINT_LOG", "data/apipoint_calls.jsonl"))
        bal, ts = None, ""
        if path.exists():
            for line in reversed(path.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("balance") is not None:
                    bal, ts = float(rec["balance"]), str(rec.get("ts") or "")
                    break
        if bal is None:
            out.append({"label": "Баланс apipoint", "value": "н/д",
                        "sub": "запросов ещё не было", "tone": ""})
            return out
        # ≈32 ₽ за полный отчёт — считаем, на сколько ещё хватит. Это понятнее
        # рублей: «13 отчётов» говорит о риске, «419 ₽» — нет.
        left = int(bal // 32)
        tone = "bad" if left < 20 else "" if left < 60 else "good"
        when = ""
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            mins = int((datetime.now(timezone.utc) - t).total_seconds() // 60)
            when = ("только что" if mins < 2 else
                    f"{mins} мин назад" if mins < 90 else f"{mins // 60} ч назад")
        except Exception:  # noqa: BLE001
            pass
        sub = f"хватит на ~{left} отчётов" + (f" · замер {when}" if when else "")
        if left < 20:
            sub = f"ХВАТИТ НА ~{left} ОТЧЁТОВ — при нуле сайт откажет в оплате"
        out.append({"label": "Баланс apipoint",
                    "value": f"{bal:,.2f}".replace(",", " ") + " ₽",
                    "sub": sub, "tone": tone})
    except Exception:  # noqa: BLE001
        pass
    return out


def _sec_between(ts_a: str, ts_b: str) -> float | None:
    """Секунд между двумя ISO-таймстемпами (b - a). None при сбое парсинга."""
    def _p(s):
        try:
            from datetime import datetime
            return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    a, b = _p(ts_a), _p(ts_b)
    return (b - a).total_seconds() if (a and b) else None


def _human_dur(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}с"
    if sec < 3600:
        return f"{sec // 60}м {sec % 60:02d}с"
    if sec < 86400:
        return f"{sec // 3600}ч {(sec % 3600) // 60}м"
    return f"{sec // 86400}д {(sec % 86400) // 3600}ч"


# Момент, когда плечи B и C начали показывать СВОИ экраны цены. До него все три
# видели один и тот же экран, и те показы к тесту отношения не имеют — иначе
# первые дни сравнивались бы разные вещи под одними буквами.
VISUAL_TEST_SINCE = "2026-08-05T18:12:00"


def _price_screen_stats(since: str, src: str = "all", campaign: str = "") -> dict[str, dict]:
    """Воронка ЭКРАНА ЦЕНЫ в разрезе плеча A/B/C.

    Считаем по УНИКАЛЬНЫМ СЕССИЯМ: `price_shown` срабатывает при каждой отрисовке,
    и человек, переключившийся с полного тарифа на объектный, дал бы два показа —
    знаменатель бы распух, а конверсия просела на ровном месте.

    Оплаты берём из реестра платежей по полю variant (там же лежит сумма) — это
    честнее, чем событие «нажали оплатить»: до ЮKassa доходят не все.
    """
    out: dict[str, dict] = {k: {"seen": 0, "clicked": 0, "paid": 0, "revenue": 0}
                            for k in CS_VARIANTS}
    seen_sids: dict[str, set] = {k: set() for k in CS_VARIANTS}
    click_sids: dict[str, set] = {k: set() for k in CS_VARIANTS}
    path = orders.PAYMENTS_LOG.parent / "analytics_events.jsonl"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if since and str(e.get("ts") or "") < since:
                    continue
                ab = str(e.get("ab") or "")
                if ab not in out:
                    continue
                if src in ("ad", "organic") and (e.get("src") or "organic") != src:
                    continue
                if campaign and (e.get("camp") or "") != campaign:
                    continue
                sid = e.get("sid") or ""
                name = e.get("name") or ""
                if name == "price_shown":
                    seen_sids[ab].add(sid)
                elif name in ("pay_click", "object_only_pay"):
                    click_sids[ab].add(sid)
    for k in out:
        out[k]["seen"] = len(seen_sids[k])
        # Клик считаем только у тех, кто дошёл до экрана цены: иначе в числителе
        # окажутся сессии без знаменателя и конверсия улетит выше 100%.
        out[k]["clicked"] = len(click_sids[k] & seen_sids[k])
    for p in orders.read_payments():
        if p.get("status") not in ("succeeded", "bypass") or p.get("test"):
            continue
        if since and str(p.get("paid_at") or "") < since:
            continue
        ab = str(p.get("variant") or "")
        if ab not in out:
            continue
        if src in ("ad", "organic") and (p.get("src") or "organic") != src:
            continue
        if campaign and (p.get("campaign") or "") != campaign:
            continue
        out[ab]["paid"] += 1
        out[ab]["revenue"] += int(p.get("amount") or 0)
    return out


def _an_panels(ctx: dict) -> list[dict]:
    """Проектные панели единого дашборда: A/B-тест цены, продажи по тарифам,
    вовлечённость (Метрика), риск-распределение, кампании, сегмент объекта, поведение.
    Рендерятся вендор-модулем в общем стиле."""
    src = ctx.get("src") if ctx.get("src") in ("all", "ad", "organic") else "all"
    tok = ctx.get("token", "")
    camp_sel = ctx.get("campaign") or "all"           # drill-down: выбрана кампания в матрице
    camp_active = camp_sel not in ("all", None, "")
    ledger = orders.PAYMENTS_LOG.parent
    # Свои проверочные оплаты вон и отсюда: иначе они попадут в CVR плеча A/B,
    # где каждая оплата на счету и одна лишняя двигает вывод.
    payments = [p for p in orders.read_payments() if not p.get("test")]
    # A/B в разрезе кампании (если выбрана) — назначения из by_camp, платежи по campaign
    if camp_active:
        counts, since = abstats.read_assignments(ledger, "all", campaign=camp_sel)
        since = max(since or "", PRODUCT_SINCE)
        stats = abstats.build_stats(list(CS_VARIANTS.keys()), counts, since, payments, "all", campaign=camp_sel)
    else:
        counts, since = abstats.read_assignments(ledger, src)
        # Панели считали от старта A/B-назначений (24.07) и переключатель периода их
        # не касался: в таблице тарифов висело «с 2026-07-24», пока весь остальной
        # дашборд показывал сегодня. Сажаем на тот же пол данных.
        since = max(since or "", PRODUCT_SINCE)
        stats = abstats.build_stats(list(CS_VARIANTS.keys()), counts, since, payments, src)
    ad_c, _ = abstats.read_assignments(ledger, "ad")
    org_c, _ = abstats.read_assignments(ledger, "organic")
    ad_n, org_n = sum(ad_c.values()), sum(org_c.values())
    panels: list[dict] = []

    # --- Экран цены: визуальный тест (раунд 4, с 05.08.2026) ------------------
    # Считаем ОТ УВИДЕВШИХ ЦЕНУ, а не от назначенных: до экрана цены доходит
    # меньше половины, и деление на всех занижало результат в разы — на этом
    # уже обожглись в ценовом раунде. Знаменатель — уникальные сессии с
    # price_shown, иначе повторный показ (человек переключил тариф) задвоит его.
    try:
        # Пол теста: до VISUAL_TEST_SINCE плечи показывали ОДИН экран.
        vstats = _price_screen_stats(max(since or "", VISUAL_TEST_SINCE),
                                     src, camp_sel if camp_active else "")
        seen_total = sum(v["seen"] for v in vstats.values())
        if seen_total:
            names = {"A": "A · текущий экран", "B": "B · заключение создано",
                     "C": "C · он знает — вы нет"}
            rows = []
            for key in sorted(vstats):
                v = vstats[key]
                seen, clicked, paid, rev = v["seen"], v["clicked"], v["paid"], v["revenue"]
                rows.append([
                    names.get(key, key), seen,
                    f'{clicked} · {clicked / seen * 100:.0f}%' if seen else "—",
                    f'{paid} · {paid / seen * 100:.1f}%' if seen else "—",
                    f'{rev / seen:.0f} ₽' if seen else "—",
                ])
            panels.append({
                "title": "Экран цены — визуальный тест", "icon": "target",
                "tag": f"с {VISUAL_TEST_SINCE[:10]}",
                "table": {"head": ["Плечо", "Увидели цену", "Нажали оплату", "Оплатили", "₽ с показа"],
                          "rows": rows},
                "hint": (
                    "Цена у всех плеч одна — различается только экран цены. "
                    "<b>A</b> — текущий («всё готово, запускаем проверку»), "
                    "<b>B</b> — «заключение уже создано» с бланком, "
                    "<b>C</b> — «он знает — вы нет». "
                    "Знаменатель — сессии, которые ДОШЛИ до экрана цены, а не все назначенные. "
                    "Решать по столбцу <b>₽ с показа</b>: конверсия без денег обманчива. "
                    "Ориентир значимости — от 250–300 показов на плечо; раньше разница "
                    "почти наверняка шум."),
            })
    except Exception:  # noqa: BLE001 — панель не имеет права ронять дашборд
        pass
    def _seg(key, label):  # переключатель источника сохраняет выбранную кампанию
        return {"label": label, "active": src == key and not camp_active,
                "href": f"/admin/stats?token={tok}&src={key}&campaign=all"}

    # --- Продажи по тарифам ---
    tariffs = stats.get("tariffs") or {}
    if tariffs:
        labels = {"base": "Полный отчёт (с продавцом)", "object": "Проверка объекта",
                  "addon_seller": "Апселл продавца", "addon": "ИНН-апселл", "ext": "Расширенная",
                  "kit": "Пакет к сделке",
                  "monitor": "Мониторинг 30д", "realtor": "Пакет риелторам", "?": "без тарифа"}
        # Разбивка базового тарифа: цена = база + расширенный апселл (BUMP_PRICE).
        # Приоритет — сохранённый флаг bump (точный). Для старых платежей без флага —
        # фолбэк по сумме (amount > цены варианта на величину BUMP_PRICE).
        base_only_rev = bump_n = bump_extra_rev = base_bump_purch = 0
        # Пакет к сделке — то же самое у объектного тарифа: цена = объект + DEALKIT_PRICE.
        # Без разбивки объектная строка выглядела бы дороже, чем стоит сама проверка.
        obj_only_rev = kit_n = kit_extra_rev = 0
        _since_dt = abstats._parse_dt(since or "")
        for p in payments:
            if p.get("status") != "succeeded" or (p.get("tariff") or "base") != "base":
                continue
            # то же окно, что и tariffs (иначе разбивка base+bump разъедется с count из stats)
            _pdt = abstats._parse_dt(p.get("paid_at") or "")
            if _since_dt is not None and not (_pdt is not None and _pdt >= _since_dt):
                continue
            # #5: тот же фильтр src/кампании, что и у stats["tariffs"] (build_stats) — иначе
            # count отфильтрован по сегменту, а base_only_rev/bump — по всему трафику (расходятся).
            _rsrc = "ad" if (p.get("src") == "ad") else "organic"
            if src in ("ad", "organic") and _rsrc != src:
                continue
            if camp_active and (p.get("campaign") or "") != camp_sel:
                continue
            amt = int(p.get("amount") or 0)
            bp = CS_VARIANTS.get(p.get("variant") or "", 0)
            has_flag = "bump" in p
            took_bump = bool(p.get("bump")) if has_flag else (bool(bp) and amt > bp)
            if took_bump:
                bump_n += 1
                extra = BUMP_PRICE if (has_flag and bp) else (amt - bp if bp else BUMP_PRICE)
                bump_extra_rev += max(0, extra)
                base_only_rev += amt - max(0, extra)
                base_bump_purch += 1
            else:
                base_only_rev += amt
        # то же по объектному тарифу
        for p_ in payments:
            if p_.get("status") != "succeeded" or (p_.get("tariff") or "") != "object":
                continue
            _pdt = abstats._parse_dt(p_.get("paid_at") or "")
            if _since_dt is not None and not (_pdt is not None and _pdt >= _since_dt):
                continue
            _rsrc = "ad" if (p_.get("src") == "ad") else "organic"
            if src in ("ad", "organic") and _rsrc != src:
                continue
            if camp_active and (p_.get("campaign") or "") != camp_sel:
                continue
            amt = int(p_.get("amount") or 0)
            if p_.get("kit"):
                kit_n += 1
                kit_extra_rev += DEALKIT_PRICE
                obj_only_rev += max(0, amt - DEALKIT_PRICE)
            else:
                obj_only_rev += amt
        order = ["base", "object", "addon", "ext", "monitor", "realtor", "?"]
        keys = [k for k in order if k in tariffs] + [k for k in tariffs if k not in order]
        trows = []
        for k in keys:
            if k == "base" and bump_n:
                # базовый разбиваем на «чистую базу» и «расширенный апселл»
                trows.append([{"html": "Базовый отчёт <span style='color:#8b93a1'>(база)</span>"},
                              tariffs["base"]["count"], f"{base_only_rev} ₽"])
                trows.append([f"→ Расширенный (+{BUMP_PRICE} ₽, залоги ФНП)", bump_n, f"{bump_extra_rev} ₽"])
            elif k == "object" and kit_n:
                trows.append([{"html": "Проверка объекта <span style='color:#8b93a1'>(база)</span>"},
                              tariffs["object"]["count"], f"{obj_only_rev} ₽"])
                trows.append([f"→ Пакет к сделке (+{DEALKIT_PRICE} ₽)", kit_n, f"{kit_extra_rev} ₽"])
            else:
                trows.append([labels.get(k, k), tariffs[k]["count"], f'{tariffs[k]["revenue"]} ₽'])
        base_cnt = stats["base_cnt"]
        kv = []
        if base_cnt:
            kv.append([f"Расширенный (+{BUMP_PRICE}) взяли", f'{base_bump_purch}/{base_cnt} · {base_bump_purch/base_cnt*100:.0f}%'])
        obj_cnt = (tariffs.get("object") or {}).get("count", 0)
        if obj_cnt:
            kv.append([f"Пакет к сделке (+{DEALKIT_PRICE}) взяли",
                       f'{kit_n}/{obj_cnt} · {kit_n/obj_cnt*100:.0f}%'])
        kv.append(["ИНН-апселл берут", f'{stats["addon_take"]*100:.1f}% ({stats["addon_cnt"]}/{base_cnt})'])
        kv.append(["Выручка по всем тарифам", f'{stats["total_revenue"]} ₽'])
        interest = abstats.read_counter(ledger, "addon_interest")
        hint = (f"«Расширенный (+{BUMP_PRICE})» — развилка в момент оплаты полного тарифа "
                f"(залоги/обременения ФНП): цена = {PRICE_FULL} + {BUMP_PRICE} ₽. "
                f"«Пакет к сделке (+{DEALKIT_PRICE})» — та же механика у объектного тарифа: "
                f"{PRICE_OBJECT} + {DEALKIT_PRICE} ₽, что запросить у продавца по этому объекту. "
                f"«ИНН-апселл» — отдельная допроверка по ИНН, {TARIFFS['addon']['amount']} ₽. ")
        if interest:
            conv = (stats["addon_cnt"] / interest * 100) if interest else 0
            hint += (f'Воронка ИНН-апселла: нажали «Проверить по ИНН» <b>{interest}</b> → оплатили '
                     f'<b>{stats["addon_cnt"]}</b> ({conv:.0f}%).')
        _since_tag = (since or "")[:10] if since else ""
        panels.append({"title": "Продажи по тарифам", "icon": "money",
                       "tag": (f"с {_since_tag}" if _since_tag else "за всё время"),
                       "table": {"head": ["Тариф", "Оплат", "Выручка"], "rows": trows},
                       "kv": kv, "hint": hint})

    # --- Вовлечённость (Метрика) ---
    try:
        from modules import metrika
        eng = metrika.fetch_engagement("today", "today")
        if eng:
            def _dur(sec):
                m, s = divmod(int(sec or 0), 60)
                return f"{m}м {s:02d}с" if m else f"{s}с"
            panels.append({"title": "Вовлечённость", "icon": "target", "tag": "Метрика · сегодня",
                           "tiles": [
                               {"v": eng.get("visits", 0), "k": "Визиты"},
                               {"v": eng.get("users", 0), "k": "Посетители"},
                               {"v": f'{eng.get("bounce_rate", 0)}%', "k": "Отказы"},
                               {"v": _dur(eng.get("avg_seconds", 0)), "k": "Ср. время"},
                               {"v": f'{eng.get("page_depth", 0)}', "k": "Глубина"}]})
    except Exception:  # noqa: BLE001
        pass

    # --- Качество поиска объекта: адрес→кадастр (DaData) → данные (НСПД/api-assist) ---
    # Раньше панель считала накопительные счётчики без дат и всегда показывала «всё
    # время» — переключатель периода её не касался. Теперь читаем журнал с метками.
    try:
        def _read_jsonl(name):
            f = ledger / name
            if not f.exists():
                return []
            out = []
            floor = PRODUCT_SINCE
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                ts = str(r.get("ts") or "")
                if ts and ts < floor:      # тот же пол данных, что у всего дашборда
                    continue
                out.append(r)
            return out

        look = _read_jsonl("objlookup.jsonl")
        if look:
            addr = [r for r in look if r.get("mode") == "addr"]
            cadm = [r for r in look if r.get("mode") == "cad"]
            a_tot, c_tot = len(addr), len(cadm)
            a_cad = sum(1 for r in addr if r.get("cad_found"))
            a_obj = sum(1 for r in addr if r.get("obj_found"))
            c_obj = sum(1 for r in cadm if r.get("obj_found"))
            obj_all, tot = a_obj + c_obj, a_tot + c_tot
            pct = lambda n, d: f"{n/d*100:.0f}%" if d else "—"
            kv = []
            if a_tot:
                kv.append(["По адресу — ввели", f"{a_tot}"])
                kv.append(["→ нашли кадастр (DaData)", f"{a_cad} · {pct(a_cad, a_tot)}"])
                kv.append(["→ получили данные объекта", f"{a_obj} · {pct(a_obj, a_tot)}"])
            if c_tot:
                kv.append(["По кадастру — ввели", f"{c_tot}"])
                kv.append(["→ получили данные объекта", f"{c_obj} · {pct(c_obj, c_tot)}"])
            by_nspd = sum(1 for r in look if r.get("source") == "nspd")
            by_aa = sum(1 for r in look if r.get("source") == "apiassist")
            if by_nspd or by_aa:
                kv.append(["Чей ответ: НСПД (бесплатно)", f"{by_nspd}"])
                kv.append(["Чей ответ: api-assist (платно)", f"{by_aa}"])
            panels.append({
                "title": "Поиск объекта (адрес→кадастр→данные)", "icon": "target",
                "tiles": [{"v": tot, "k": "Всего запросов"},
                          {"v": pct(obj_all, tot), "k": "Получили данные"}],
                "kv": kv,
                "hint": ("Считается для запустивших проверку. По адресу: DaData Clean даёт кадастр "
                         "квартиры → НСПД отдаёт данные. Низкий «нашли кадастр» = адреса вне покрытия "
                         "DaData или опечатки. Низкий «данные» при найденном кадастре = НСПД не отдал. "
                         "api-assist в превью " + ("ВКЛЮЧЁН как запасной." if os.getenv(
                             "NSPD_FALLBACK_APIASSIST", "").strip() in ("1", "true", "yes")
                             else "ВЫКЛЮЧЕН (NSPD_FALLBACK_APIASSIST): он тратит суточную квоту, "
                                  "поэтому здесь его доля всегда 0. Он работает после оплаты и в "
                                  "поиске квартир — см. соседнюю панель.")),
            })
        else:
            panels.append({
                "title": "Поиск объекта (адрес→кадастр→данные)", "icon": "target",
                "banner": {"tone": "info", "text": "За выбранный период проверок ещё не запускали. "
                           "Журнал ведётся с 05.08.2026 — более ранние запросы считались "
                           "счётчиками без дат и в окно не попадают."},
            })
    except Exception:  # noqa: BLE001
        pass

    # --- api-assist: запасной источник ЕГРН и поиск квартир по дому ---
    try:
        aa = _read_jsonl("apiassist.jsonl")
        from modules import apiassist as _aa
        used, limit = _aa.used_today(), _aa.day_limit()
        rescues = sum(1 for r in aa if r.get("event") == "rescue_paid")
        searches = [r for r in aa if r.get("event") == "house_search"]
        empty = sum(1 for r in searches if not r.get("found"))
        tone = "bad" if used >= limit * 0.9 else ""
        panels.append({
            "title": "api-assist (запасной источник)", "icon": "target",
            "tag": f"квота {used}/{limit} за сутки",
            "tiles": [
                {"v": rescues, "k": "Спасли оплаченных"},
                {"v": len(searches), "k": "Поисков квартир"},
                {"v": f"{used}/{limit}", "k": "Квота сегодня"},
            ],
            "kv": [
                ["Списков квартир — пусто", f"{empty} из {len(searches)}"],
                ["Резерв под платный путь", f"{_CAND_RESERVE}"],
            ],
            "hint": "«Спасли оплаченных» — объект, которого NewDB не дал, а api-assist нашёл: "
                    "без него это был бы отчёт «объекта нет в ЕГРН» по оплаченному заказу. "
                    "«Поиск квартир» — платный запрос списка помещений дома для выбора квартиры; "
                    "он же тратит суточную квоту, поэтому под платный путь держим резерв.",
        })
    except Exception:  # noqa: BLE001
        pass


    # --- Метрика 1: распределение риска в отчётах + связь с возвратами ---
    try:
        order_recs = orders.read_payments()  # записи заказов (report_id, refunded, chargeback)
        by_report = {}
        for o in order_recs:
            rid = o.get("report_id")
            if rid:
                by_report[rid] = o
        risk_tally = {"высокий": 0, "средний": 0, "низкий": 0}
        risk_refund = {"высокий": 0, "средний": 0, "низкий": 0}
        paid_reports = 0
        for jf in REPORTS_DIR.glob("*.json"):
            try:
                r = json.loads(jf.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if r.get("preview"):          # только финальные (оплаченные) отчёты
                continue
            if since and (r.get("created_at") or "") < since:
                continue                  # уважаем окно сброса статистики (с start счётчика)
            risk = r.get("risk")
            if risk not in risk_tally:
                continue
            paid_reports += 1
            risk_tally[risk] += 1
            o = by_report.get(r.get("id"))
            if o and (o.get("refunded") or o.get("chargeback")):
                risk_refund[risk] += 1
        if paid_reports:
            pct = lambda n, d: f"{n/d*100:.0f}%" if d else "—"
            labels = {"высокий": "Высокий риск", "средний": "Средний", "низкий": "Чисто (низкий)"}
            kv = []
            for k in ("высокий", "средний", "низкий"):
                n = risk_tally[k]
                ref = risk_refund[k]
                refstr = f" · возвраты {ref} ({pct(ref, n)})" if n else ""
                kv.append([labels[k], f"{n} · {pct(n, paid_reports)}{refstr}"])
            panels.append({
                "title": "Что находим в отчётах (риск)", "icon": "bars", "tag": "в выбранном окне",
                "tiles": [{"v": paid_reports, "k": "Полных проверок"},
                          {"v": pct(risk_tally["низкий"], paid_reports), "k": "«Чисто»"},
                          {"v": pct(risk_tally["высокий"] + risk_tally["средний"], paid_reports), "k": "С риском"}],
                "kv": kv,
                "hint": "Распределение риска по ВСЕМ полным проверкам (вкл. исторические до freemium — "
                        "поэтому число большое, это не покупки). ⚠️ «средний» завышен: при пустом балансе "
                        "NewDB база не отвечает → риск ставится «средний». Возвраты считаются только по реально оплаченным.",
            })
    except Exception:  # noqa: BLE001
        pass

    # (панель «Выручка по кампаниям» убрана — «Матрица кампаний» вендор-модуля показывает
    #  выручку по campaign в том же окне since + с exclude_campaigns; две таблицы с разными
    #  числами на одной странице путали.)

    # --- Метрика 4: сегмент объекта (тип / регион / кад.стоимость) ---
    try:
        of = ledger / "object_stats.jsonl"
        if of.exists():
            _RU_REG = {"77": "Москва", "50": "Московская обл.", "78": "Санкт-Петербург",
                       "23": "Краснодарский край", "66": "Свердловская обл.", "16": "Татарстан",
                       "54": "Новосибирская обл.", "52": "Нижегородская обл."}
            types, regions, costs = {}, {}, []
            for line in of.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                t = (r.get("type") or "—").strip() or "—"
                types[t] = types.get(t, 0) + 1
                reg = r.get("region") or ""
                if reg:
                    name = _RU_REG.get(reg, f"регион {reg}")
                    regions[name] = regions.get(name, 0) + 1
                try:
                    cv = float(r.get("cost") or 0)
                    if cv > 0:
                        costs.append(cv)
                except Exception:  # noqa: BLE001
                    pass
            total_obj = sum(types.values())
            if total_obj:
                type_bars = sorted(types.items(), key=lambda kv: -kv[1])[:6]
                reg_bars = sorted(regions.items(), key=lambda kv: -kv[1])[:6]
                kv = []
                if costs:
                    costs.sort()
                    med = costs[len(costs) // 2]
                    kv.append(["Кад. стоимость (медиана)", f"{med/1e6:.1f} млн ₽"])
                    kv.append(["Диапазон", f"{min(costs)/1e6:.1f}–{max(costs)/1e6:.1f} млн ₽"])
                panel = {"title": "Сегмент объекта (рынок)", "icon": "target", "tag": "по данным НСПД",
                         "tiles": [{"v": total_obj, "k": "Объектов с данными"}],
                         "bars": type_bars, "kv": kv,
                         "hint": "Что и где проверяют + ценовой сегмент. Под это затачивать продукт и цену."}
                if reg_bars:
                    panel["hint"] = ("Тип объекта (бары). Топ-регионы: "
                                     + " · ".join(f"{k} {v}" for k, v in reg_bars) + ". " + panel["hint"])
                panels.append(panel)
    except Exception:  # noqa: BLE001
        pass

    # --- Метрика 6: время до оплаты + повторные вводы объекта (из событий) ---
    try:
        ev = ledger / "analytics_events.jsonl"
        if ev.exists():
            first_visit, first_pay, cs_count = {}, {}, {}
            for line in ev.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                did, nm, ts = r.get("sid"), r.get("name"), r.get("ts") or ""
                if not did:
                    continue
                if nm == "visit" and did not in first_visit:
                    first_visit[did] = ts
                elif nm == "pay_click" and did not in first_pay:
                    first_pay[did] = ts
                elif nm == "check_started":
                    cs_count[did] = cs_count.get(did, 0) + 1
            deltas = []
            for did, pt in first_pay.items():
                vt = first_visit.get(did)
                d = _sec_between(vt, pt)
                if d is not None and 0 <= d < 7 * 24 * 3600:
                    deltas.append(d)
            repeat_did = sum(1 for c in cs_count.values() if c > 1)
            started_did = len(cs_count)
            if deltas or started_did:
                kv = []
                if deltas:
                    deltas.sort()
                    med = deltas[len(deltas) // 2]
                    kv.append(["Время до оплаты (медиана)", _human_dur(med)])
                    kv.append(["Быстрее всех / дольше всех", f"{_human_dur(min(deltas))} / {_human_dur(max(deltas))}"])
                if started_did:
                    pct = lambda n, d: f"{n/d*100:.0f}%" if d else "—"
                    kv.append(["Вводили объект повторно", f"{repeat_did} из {started_did} · {pct(repeat_did, started_did)}"])
                panels.append({
                    "title": "Решение и колебания", "icon": "clock", "tag": "поведение",
                    "kv": kv,
                    "hint": "Долго думают → добавить срочность/гарантию. Много повторных вводов объекта = "
                            "путаются с форматом кадастра/адреса (упростить подсказку).",
                })
    except Exception:  # noqa: BLE001
        pass

    # --- Расходы NewDB (платные проверки) — куда уходят деньги с баланса ---
    try:
        calls_f = ledger / "newdb_calls.jsonl"
        if calls_f.exists():
            by_method: dict[str, dict] = {}
            total = fails = 0
            method_ru = {"fssp_person": "ФССП (произв-ва)", "bankrot_person": "Банкротство",
                         "arbitr_person": "Арбитраж", "passport_fns": "Паспорт+ИНН",
                         "pledge_person": "Залоги"}
            # Панель показывала «всего за всё время» и отдельной плиткой «в окне»,
            # а разбивка по методам считалась по всей истории — то есть по деньгам,
            # потраченным ещё при других ценах и другом коде. Считаем только окно.
            for line in calls_f.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                ts = str(r.get("ts") or "")
                if since and ts < since:
                    continue
                m = r.get("method", "?")
                a = by_method.setdefault(m, {"ok": 0, "fail": 0})
                if r.get("ok"):
                    a["ok"] += 1
                    total += 1
                else:
                    a["fail"] += 1
                    fails += 1
            if total or fails:
                kv = [[method_ru.get(m, m), f'{v["ok"]} платных'
                       + (f' · {v["fail"]} отклонено' if v["fail"] else "")]
                      for m, v in sorted(by_method.items(), key=lambda kv: -kv[1]["ok"])]
                panels.append({
                    "title": "Расходы NewDB (платные проверки)", "icon": "money",
                    # Баланс не дублируем: он уже в верхней сводке отдельной плиткой.
                    "tiles": [{"v": total, "k": "Платных запросов"},
                              {"v": fails, "k": "Отклонено (без списания)"}],
                    "kv": kv,
                    "hint": "Каждый платный запрос = списание с баланса NewDB. Полная проверка одного "
                            "продавца = несколько методов (ФССП+банкротство+арбитраж+…). Резкое падение "
                            "баланса без роста запросов здесь = внешние/чужие траты (см. ЛК NewDB).",
                })
    except Exception:  # noqa: BLE001
        pass

    # --- Развилка воронки: после email путь расходится (с ФИО → продавец / без ФИО → объект) ---
    try:
        since = _an_since()
        ev = ledger / "analytics_events.jsonl"
        if ev.exists():
            want = {"wizard_email", "wizard_fio", "wizard_dob", "pay_click",
                    "object_only_chosen", "object_only_pay"}
            seen: dict[str, set] = {n: set() for n in want}
            sid_ev: dict[str, set] = {}
            for line in ev.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if since and (r.get("ts") or "") < since:
                    continue
                if (r.get("camp") or "") in (ANALYTICS_CFG.exclude_campaigns or set()):
                    continue  # #14: исключённые кампании (sdelka_rsya) — вон из Развилки
                nm, sid = r.get("name"), r.get("sid") or ""
                if not sid or nm not in want:
                    continue
                seen[nm].add(sid)
                sid_ev.setdefault(sid, set()).add(nm)
            c = {n: len(seen[n]) for n in want}
            # НАЖАЛИ оплату (интент, по событиям): продавец = pay_click без object_only_pay.
            seller_click = len({s for s, e in sid_ev.items()
                                if "pay_click" in e and "object_only_pay" not in e})
            object_click = c["object_only_pay"]
            # ОПЛАТИЛИ (факт, из реестра платежей в окне): object-тариф vs base (путь продавца).
            _since_dt = abstats._parse_dt(since or "")
            obj_paid = seller_paid = 0
            # #5/#6: Развилка — обзор ВЕТВЛЕНИЯ путей по ВСЕМУ трафику (обе метрики глобальны:
            # интент из событий не имеет надёжной src-метки, поэтому и факт не сегментируем —
            # иначе дисбаланс интент↔факт). Исключаем только exclude_campaigns (#14, sdelka_rsya).
            _excl = ANALYTICS_CFG.exclude_campaigns or set()
            for p in payments:
                if p.get("status") != "succeeded":
                    continue
                pdt = abstats._parse_dt(p.get("paid_at") or "")
                if _since_dt is not None and not (pdt is not None and pdt >= _since_dt):
                    continue
                if (p.get("campaign") or "") in _excl:  # #14: исключённые кампании вон
                    continue
                tk = (p.get("tariff") or "base")
                if tk == "object":
                    obj_paid += 1
                elif tk == "base":
                    seller_paid += 1
            if c["wizard_email"] or c["object_only_chosen"] or c["wizard_fio"]:
                def _mini(steps, top):
                    top = top or 1
                    out = ""
                    for lbl, n in steps:
                        w = max(2, round(n / top * 100))
                        pct = f'{round(n/top*100)}%' if top else ''
                        out += (f'<div style="margin-bottom:9px"><div style="display:flex;justify-content:space-between;'
                                f'font-size:12px;margin-bottom:3px"><span>{lbl}</span><b style="font-variant-numeric:tabular-nums">{n}</b></div>'
                                f'<div style="height:7px;background:#eef1f6;border-radius:4px;overflow:hidden">'
                                f'<i style="display:block;height:100%;width:{w}%;background:{"#2563eb"}"></i></div></div>')
                    return out
                base = c["wizard_email"] or 1
                left = _mini([("Ввели email", c["wizard_email"]),
                              ("Ввели ФИО", c["wizard_fio"]),
                              ("Ввели дату рожд.", c["wizard_dob"]),
                              ("Нажали оплату", seller_click),
                              ("✓ Оплатили (факт)", seller_paid)], base)
                right = _mini([("Ввели email", c["wizard_email"]),
                               ("«Не знаю ФИО»", c["object_only_chosen"]),
                               ("Нажали оплату", object_click),
                               ("✓ Оплатили (факт)", obj_paid)], base)
                col = ('<div style="font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;'
                       'margin-bottom:10px;color:{color}">{title}</div>{body}')
                two = ('<div style="display:grid;grid-template-columns:1fr 1fr;gap:22px">'
                       + '<div style="border-right:1px solid #eef1f6;padding-right:20px">'
                       + col.format(color="#1d4ed8", title="С ФИО → продавец", body=left) + '</div>'
                       + '<div>' + col.format(color="#b45309", title="Без ФИО → объект", body=right) + '</div></div>')
                panels.append({
                    "title": "Развилка воронки", "icon": "funnel", "tag": "весь трафик · после email",
                    "table": {"head": [], "rows": [[{"html": two}]]},
                    "hint": (f"После шага «email» путь расходится: у кого есть ФИО продавца — идут в полную "
                             f"проверку ({PRICE_FULL} ₽), у кого нет — берут проверку только объекта "
                             f"({PRICE_OBJECT} ₽) и могут дозаказать продавца потом ({SELLER_ADDON_PRICE} ₽). «Нажали оплату» = клик по кнопке (интент, по "
                             "событиям); «✓ Оплатили (факт)» = ПЕРВИЧНЫЕ прошедшие платежи по этой ветке "
                             "(полный/объектный, из реестра). Разница интент↔факт = отвал на странице ЮKassa. "
                             "Плитка ОПЛАТЫ считает ВСЕ платежи, включая апселлы (продавец/ИНН) — поэтому её "
                             "число может быть больше суммы двух колонок здесь."),
                })
    except Exception:  # noqa: BLE001
        pass

    # ---- Срезы воронки: кто именно отваливается ----
    # Общая конверсия — одно число без объяснения. Разложенная по устройству, ОС и
    # браузеру, она сразу показывает, где не «люди не убедились», а «страница не работает».
    try:
        panels.extend(_breakdown_panels(since))
    except Exception as e:  # noqa: BLE001 — панель не имеет права ронять дашборд
        print(f"[panels] срезы пропущены: {type(e).__name__}: {e}", flush=True)

    return panels


_BREAKDOWNS = (("dev", "Устройство"), ("os", "ОС"), ("br", "Браузер"),
               ("src", "Источник"), ("reg", "Регион объекта"), ("ref", "Откуда перешли"))
# Среза по плечу A/B здесь больше нет: цены во всех плечах одинаковы с 05.08.2026,
# и таблица сравнивала бы одно и то же с самим собой. Вернуть, когда плечи снова
# начнут различаться — тогда срез отвечает, ГДЕ именно вариант теряет людей.


def _breakdown_panels(since: str | None) -> list[dict]:
    """Воронка в разрезе устройства/ОС/браузера/источника/региона.

    Считаем по уникальным сессиям, а не по событиям: иначе один человек, десять раз
    нажавший «Проверить», выглядел бы как десять заинтересованных.
    """
    ev = orders.PAYMENTS_LOG.parent / "analytics_events.jsonl"
    if not ev.exists():
        return []
    excl = ANALYTICS_CFG.exclude_campaigns or set()
    # sid → {срез: (значение, момент, когда срез стал известен)}
    dims: dict[str, dict[str, tuple[str, str]]] = {}
    # sid → {шаг: самое раннее время}
    steps: dict[str, dict[str, str]] = {}
    for line in ev.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        ts = r.get("ts") or ""
        if since and ts < since:
            continue
        if (r.get("camp") or "") in excl:
            continue
        sid = r.get("sid") or ""
        if not sid:
            continue
        d = dims.setdefault(sid, {})
        for key, _ in _BREAKDOWNS:
            if r.get(key) and key not in d:
                d[key] = (str(r[key]), ts)
        nm = r.get("name")
        if nm in ("visit", "check_completed", "pay_click"):
            s = steps.setdefault(sid, {})
            if nm not in s:
                s[nm] = ts

    out: list[dict] = []
    for key, label in _BREAKDOWNS:
        agg: dict[str, list[int]] = {}
        for sid, d in dims.items():
            got = d.get(key)
            if not got:
                continue
            value, known_since = got
            row = agg.setdefault(value, [0, 0, 0])
            row[0] += 1
            # Считаем только то, что случилось ПОСЛЕ того, как срез стал известен.
            # Иначе выходит так: человек платит ночью (среза ещё нет, выкатки не было),
            # днём заходит снова — и его старый клик по оплате задним числом получает
            # ярлык «смартфон» и всплывает в таблице. Со стороны это выглядит как
            # новая оплата, хотя денег не прибавилось: ровно на этом мы и споткнулись.
            s = steps.get(sid) or {}
            if s.get("check_completed") and s["check_completed"] >= known_since:
                row[1] += 1
            if s.get("pay_click") and s["pay_click"] >= known_since:
                row[2] += 1
        if not agg:
            continue
        rows = []
        for value, (n, p, c) in sorted(agg.items(), key=lambda kv: -kv[1][0])[:12]:
            rows.append([value, n, p, f"{100 * p / n:.0f}%" if n else "—",
                         c, f"{100 * c / n:.2f}%" if n else "—"])
        out.append({
            "title": f"Срез: {label}", "icon": "funnel",
            "tag": f"{len(agg)} значений",
            "table": {"head": [label, "Сессий", "Превью", "→ превью",
                               "Нажали оплату", "→ к оплате"],
                      "rows": rows},
            "hint": ("Сессии, а не события: один человек считается один раз. Столбец — клики "
                     "по кнопке оплаты (НАМЕРЕНИЕ), а не деньги: факт оплаты приходит уже "
                     "без браузера, и в этой таблице его быть не может. Деньги — в «Продажах "
                     "по тарифам». Считаем только то, что случилось после того, как срез стал "
                     "известен: иначе давний клик задним числом получал бы ярлык при новом "
                     "заходе того же человека и выглядел бы как свежая оплата."),
        })
    return out


def _an_derive(name: str, body: dict[str, Any]) -> dict[str, str]:
    """Регион ОБЪЕКТА (не посетителя) на события ввода объекта.

    Для сервиса про недвижимость география спроса — это где стоит квартира, а не
    откуда зашли: человек из Москвы проверяет дом в Сочи, и ставку в Директе надо
    двигать по второму, а не по первому. Геолокация по IP нам этого не скажет,
    а кадастровый номер скажет точно.
    """
    ref = str((body or {}).get("object") or "").strip()
    if not ref:
        return {}
    from modules import fssp, rosreestr
    code = (fssp.region_from_kadastr(ref) if rosreestr.is_kadastr(ref)
            else fssp.region_from_address(ref))
    return {"reg": fssp.region_name(code)} if code else {}


def _record_consent(rec: dict[str, Any], request: Request, text: str = "",
                    version: str = "") -> None:
    """Зафиксировать согласие так, чтобы его можно было доказать через год.

    Галочки на экране больше нет — согласие даётся нажатием кнопки, поэтому
    единственная защита это запись. Раньше писали только consent=True и время:
    доказать, С ЧЕМ именно человек согласился, было нечем. В деле Московского
    УФАС чекбокс отклонили ровно по этой причине — «согласие давали
    неустановленные лица». Пишем всё, что доступно на стороне сервера.
    """
    rec["consent"] = True
    rec["consent_at"] = datetime.now(timezone.utc).isoformat()
    rec["consent_method"] = "button_click"
    if text:
        rec["consent_text"] = str(text)[:600]        # дословно то, что видел человек
    if version:
        rec["consent_version"] = str(version)[:20]   # редакция формулировки
    try:
        rec["consent_ip"] = _client_ip(request)
        rec["consent_ua"] = (request.headers.get("user-agent") or "")[:300]
        rec["consent_page"] = (request.headers.get("referer") or "")[:300]
        rec["consent_lang"] = (request.headers.get("accept-language") or "")[:60]
    except Exception as e:  # noqa: BLE001 — фиксация не имеет права ронять оплату
        print(f"[consent] не всё записано: {type(e).__name__}: {e}", flush=True)


def _order_context(request: Request, report_id: str) -> dict[str, str]:
    """Срезы заказа для реестра оплат: устройство, тип ввода и регион объекта.

    Реестр знал только деньги (сумма/тариф/вариант/кампания), поэтому вопросы вида
    «мобильные платят меньше?» и «участки хуже квартир?» приходилось проверять по
    событиям, где нет сумм. Пишем на заказе, а не на платеже: к моменту оплаты
    запроса от браузера уже нет — человек возвращается из ЮKassa.
    """
    from modules.analytics import classify_object, ua_dims
    out = {"device": ua_dims(request.headers.get("user-agent") or "")["dev"]}
    # Заказ из режима «не считать меня» помечаем тестовым: если владелец доведёт
    # проверку до реальной оплаты, деньги не должны попасть в выручку и в A/B.
    if request.cookies.get("cs_notrack"):
        out["test"] = "1"
    rec = _load_report(report_id) if report_id else None
    ref = (rec or {}).get("object_ref") or ""
    if ref:
        from modules import fssp, rosreestr
        is_kad = rosreestr.is_kadastr(ref)
        out["obj_kind"] = "кадастр" if is_kad else classify_object(ref)
        code = fssp.region_from_kadastr(ref) if is_kad else fssp.region_from_address(ref)
        if code:
            out["region"] = fssp.region_name(code)
    return out

# ЦЕНА ЗАФИКСИРОВАНА 05.08.2026 — ценовой тест закрыт, плечи уходят под визуальный.
#
# Что показал раунд 3 (24.07–04.08), при правильном знаменателе «кто увидел ИМЕННО
# эту цену», а не «все назначенные»:
#   объект  249 → 21,7% CVR / 54,1 ₽ с посетителя
#           349 → 19,7% / 68,6 ₽   ← лучшее, разница в конверсии с 249 недостоверна (p=0,58)
#           449 → 11,9% / 53,3 ₽   ← обрыв, достоверный против 349 (p=0,020)
#   полный  499 → 23,9% / 151,8 ₽  ← лучшее
#           599 → 10,8% / 73,2 ₽   (немонотонно, на 120 наблюдениях — шум)
#           699 → 15,6% / 133,3 ₽
#
# Приз лежит не в цене: 213 человек увидели её и только 41 нажал «Оплатить».
#
# 05.08.2026, вечер — объект снижен до 299 (решение владельца). Аргумент не в
# прямой выручке: по замерам выше 349 давал ~51 ₽ с показа, 299 по интерполяции
# даёт ~48, и различить их на наших выборках нельзя. Аргумент в том, что каждый
# лишний покупатель объекта — вход в апселлы (пакет, продавец, ИНН), а этого
# таблица цен не видит: она считает только первый платёж. Плюс в день фиксации
# 349 дал 8,1% вместо обещанных тестом 14,6% — одна точка и 7 оплат, но
# направление настораживает.
# АВТО. Два тарифа, и делятся они не по объёму, а по СВЕЖЕСТИ данных — это
# честнее и заодно единственное, чем мы отличаемся от Автотеки с Автокодом.
#
# 199 ₽ — только то, что проверяется в реальном времени: залог в реестре ФНП и
# лизинг в Федресурсе. Себестоимость 4,90 ₽ (vindecode 1,10 + notary 1,90 +
# fedresurs 1,90), и ни одной оговорки про архивные выгрузки. Это же и главный
# риск покупки: заложенную машину банк забирает по ст. 353 ГК, и осмотр на
# подъёмнике от этого не спасает.
#
# 449 ₽ — все восемь блоков. Сюда входят сведения ГИБДД, которые сейчас
# приходят из архивных выгрузок, поэтому у каждой строки в отчёте стоит дата.
# Себестоимость 50 ₽ (reportjson; поллинг у поставщика бесплатен — замерено).
PRICE_OBJECT = 199          # «Проверка на залог» — только актуальные данные
PRICE_FULL = 449            # «Полная проверка» — все блоки, часть с датами


from modules.analytics import AnalyticsConfig, make_router as _an_router  # noqa: E402


ANALYTICS_CFG = AnalyticsConfig(
    project_id="sdelka",
    data_dir=str(orders.PAYMENTS_LOG.parent),
    admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
    title="ЧистаяСделка · продуктовая аналитика",
    # Открываем на «сегодня». 05.08.2026 сменились обе цены, обе развилки и три
    # апселла — в одном окне со старыми данными это среднее по разным продуктам.
    default_period="today",
    # Данных до 05.08 в дашборде нет вовсе: в этот день сменились обе цены, обе
    # развилки и три апселла. Смешивать их со старыми — считать средним по двум
    # разным продуктам. История никуда не делась: она в PRICING.md и в логах.
    data_floor=PRODUCT_SINCE,
    # Воронка ПОШАГОВОГО МАСТЕРА (превью объекта → мастер вовлечения email→ФИО→ДР → оплата).
    # Гранулярно видно, на каком именно поле отваливаются (раньше был один обвал 158→5).
    funnel=[("visit", "Зашли на сайт"),
            ("check_started", "Ввели объект"),
            ("check_completed", "Увидели превью объекта"),
            ("wizard_start", "Нажали «Проверить продавца»"),
            ("wizard_email", "Шаг 1 · ввели email"),
            ("wizard_fio", "Шаг 2 · ввели ФИО"),
            ("wizard_dob", "Шаг 3 · ввели дату рожд."),
            # Шага «увидели цену» в воронке НЕ БЫЛО, а именно на нём уходит 80%
            # дошедших: 269 из 327 в полном тарифе и 530 из 663 в объектном.
            # Событие единое для обеих веток (см. price_shown в index.html).
            ("price_shown", "Увидели ЦЕНУ"),
            ("pay_click", "Нажали «Оплатить»")],
    money=[("pay_click", "Нажали «Оплатить»"), ("yookassa_reached", "Дошли до ЮKassa")],
    roles={"obj_split": "check_started", "abandon": "check_abandoned",
           "friction": "checkout_abandon", "lift": "preview_object", "bump": "pay_click",
           "completed": "check_completed"},
    heartbeat_event="check_heartbeat",   # пинг «я тут» раз в 5с → last-seen секунда ухода
    campaign_cookie="cs_camp",           # штампуем события кампанией → матрица + drill-down
    # Срезы на каждом событии: устройство/ОС/браузер из UA, источник и плечо A/B из кук,
    # регион ОБЪЕКТА — из кадастрового номера или адреса.
    stamp_device=True,
    stamp_cookies={"cs_src": "src", "cs_v2": "ab"},
    derive_fields=_an_derive,
    exclude_campaigns={"sdelka_rsya"},   # РСЯ на паузе, но липкая кука капает — вон из статы
    exclude_cookie="cs_notrack",         # свои заходы (/?notrack=1) не пишем вовсе
    # Цена одна для всех плеч с 05.08.2026 — ценовой тест закрыт (см. панель «Цены»).
    price_variants={"A": PRICE_FULL, "B": PRICE_FULL, "C": PRICE_FULL},
    payments_provider=_an_payments,
    since_provider=_an_since,
    extra_kpis_provider=_an_extra_kpis,
    panels_provider=_an_panels,
    # checkout_open — «открыли модалку» (после ввода объекта), preview_submit — «форма превью
    # отправлена»; оба как сигналы, не как шаги воронки (seller_form_shown теперь шаг воронки).
    extra_events={"yookassa_reached", "preview_object", "checkout_abandon", "monitor_started",
                  "kit_shown", "kit_yes", "kit_no", "seller_form_shown", "object_only_chosen",
                  "report_demo_view", "cta_click", "bump_shown", "preview_submit", "addr_retry",
                  "checkout_open", "check_run", "seller_form_shown",
                  "object_only_chosen", "object_only_pay",
                  # Всё, что фронт слал, а сервер молча выбрасывал (имени нет в
                  # списке — событие уходит в /dev/null без единой записи в лог).
                  "bump_yes", "bump_no", "email_left", "cta_empty_object",
                  "cand_shown", "cand_picked",
                  # Жизнь ПОСЛЕ оплаты — страница отчёта. Ровно та часть выручки,
                  # которая растёт быстрее всего, и до сих пор её не было видно.
                  "report_open", "addon_view", "addon_click", "addon_pay_click",
                  "addon_included_start", "seller_addon_view", "seller_addon_click",
                  "seller_addon_pay",
                  # Дочитывание страницы, JS-ошибки и воскрешённая цель Метрики.
                  "see_sources", "see_compare", "see_pricing", "see_faq", "faq_open",
                  "js_error", "pay_start"},
    # прочие собираемые сигналы → отдельная карточка (раньше молча отбрасывались)
    signal_labels={"report_demo_view": "Посмотрели демо-отчёт", "cta_click": "Клик по CTA",
                   "bump_shown": "Развилка расширенного · показали", "monitor_started": "Запустили мониторинг",
                   "kit_shown": "Развилка пакета · показали",
                   "kit_yes": "Пакет к сделке · взяли", "kit_no": "Пакет к сделке · отказ",
                   "price_shown": "Увидели цену",
                   "checkout_open": "Открыли модалку проверки", "seller_form_shown": "Дошли до экрана оплаты",
                   "object_only_chosen": "Выбрали «нет ФИО» → объект", "object_only_pay": "Оплата только объекта",
                   "addr_retry": "Адрес не найден → изменить объект",
                   # апселл в модалке перед оплатой: показ уже был, теперь виден и выбор
                   "bump_yes": "Расширенный · взяли", "bump_no": "Расширенный · отказ",
                   "email_left": "Оставили email", "cta_empty_object": "Клик по кнопке с пустым полем",
                   "cand_shown": "Показали список квартир", "cand_picked": "Выбрали квартиру из списка",
                   # воронка страницы отчёта: открыл → увидел апселл → кликнул → ушёл платить
                   "report_open": "Открыли готовый отчёт",
                   "addon_view": "Отчёт · показали банкротство (249₽)",
                   "addon_click": "Отчёт · клик по банкротству",
                   "addon_pay_click": "Отчёт · банкротство → оплата",
                   "addon_included_start": "Отчёт · банкротство уже входило в тариф",
                   "seller_addon_view": "Отчёт · показали проверку продавца",
                   "seller_addon_click": "Отчёт · клик по продавцу",
                   "seller_addon_pay": "Отчёт · продавец → оплата",
                   # дочитывание: докуда вообще долистывают ниже первого экрана
                   "see_sources": "Долистали до «что проверяем»",
                   "see_compare": "Долистали до сравнения",
                   "see_pricing": "Долистали до тарифов",
                   "see_faq": "Долистали до вопросов",
                   "faq_open": "Развернули вопрос",
                   "js_error": "Ошибка JS на странице",
                   "pay_start": "Создали платёж → ЮKassa"},
)
app.include_router(_an_router(ANALYTICS_CFG))


# ---------- Антибот: rate-limit по IP (in-memory) ----------

import time as _time

_RATE: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    """Реальный IP клиента за доверенным прокси (Traefik). Берём ПРАВЫЙ элемент XFF —
    его добавляет наш прокси, он не подделывается. Левые элементы клиент может подделать
    (раньше брали [0] → обход rate-limit подстановкой X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _rate_ok(key: str, limit: int, window: float) -> bool:
    now = _time.time()
    hits = [t for t in _RATE.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _RATE[key] = hits
        return False
    hits.append(now)
    _RATE[key] = hits
    return True


class CheckRequest(BaseModel):
    # ФИО/ДР необязательны: БЕСПЛАТНОЕ ПРЕВЬЮ использует только объект (данные квартиры из
    # ЕГРН), персональные данные продавца нужны лишь для платной проверки продавца (собираются
    # на шаге оплаты). object_ref — единственное обязательное поле.
    seller_name: str = Field("", description="ФИО продавца (для платной проверки продавца)")
    seller_dob: str = Field("", description="Дата рождения ДД.ММ.ГГГГ")
    object_ref: str = Field(..., min_length=3, description="Кадастровый номер или адрес")
    seller_inn: str = Field("", description="ИНН продавца (для проверки банкротства)")
    passport_seria: str = Field("", description="Серия паспорта продавца (полный тариф)")
    passport_nomer: str = Field("", description="Номер паспорта продавца (полный тариф)")
    email: EmailStr | None = None
    tariff: str = Field("base")
    consent: bool = Field(False, description="Согласие на обработку ПДн (152-ФЗ)")
    consent_text: str = Field("", max_length=600, description="Текст согласия, показанный человеку")
    consent_version: str = Field("", max_length=20, description="Редакция формулировки")


class PayRequest(BaseModel):
    report_id: str
    tariff: str = "base"
    # Данные продавца собираются ЗДЕСЬ (на шаге оплаты), а не перед бесплатным превью —
    # они нужны для проверки продавца (ФССП/ЕФРСБ/арбитраж), которая идёт после оплаты.
    seller_name: str = Field("", description="ФИО продавца")
    seller_dob: str = Field("", description="Дата рождения ДД.ММ.ГГГГ")
    email: EmailStr | None = None
    passport_seria: str = Field("", description="Серия паспорта (полный тариф)")
    passport_nomer: str = Field("", description="Номер паспорта (полный тариф)")
    consent: bool = Field(False, description="Согласие на обработку ПДн продавца (152-ФЗ)")
    consent_text: str = Field("", max_length=600, description="Текст согласия, показанный человеку")
    consent_version: str = Field("", max_length=20, description="Редакция формулировки")
    # Order bump в модалке оплаты: расширенный отчёт (+ залоги ФНП). Базовый тариф.
    bump: bool = False
    # Апселл ОБЪЕКТНОГО тарифа: пакет к сделке (что запросить у продавца).
    kit: bool = False
    # Проверка ТОЛЬКО объекта (без ФИО продавца): обременения/аресты/собственники/история
    # по ЕГРН. Дешевле, для тех, у кого нет ФИО продавца. Продавца можно дозаказать (апселл).
    object_only: bool = False


# 199 → 299 (05.08.2026). Расширенный брали 42 из 64, кому он показан, — 65,6%.
# Причём чем дороже корзина, тем ОХОТНЕЕ берут: при базе 699 ₽ доля 78,9%, при
# 499 ₽ — 68,8%. Согласие двух третей не глядя означает, что цена ниже готовности
# платить. Порог безубыточности: даже если доля упадёт до 44%, выручка не изменится.
BUMP_PRICE = 299

# Пакет к сделке — апселл для ОБЪЕКТНОГО тарифа в момент оплаты. У базового тарифа
# доплата продаёт новую базу (залоги ФНП по продавцу); по одному кадастру такой базы
# нет — все доступные сведения уже входят в цену объекта. Поэтому здесь продаётся не
# новый источник, а применение найденного: что запросить у продавца по этому объекту.
#
# 299 → 199 (05.08.2026): апселл не должен стоить дороже двух третей самого товара.
# При объекте за 299 доплата в 299 читается как «вторая покупка», а не «дополнить
# свою» — и упирается в тот же барьер, который мы только что снизили.
DEALKIT_PRICE = 199
# Проверка только объекта (без продавца) — тоже A/B/C (раунд 3, 2026-07-24), дешевле
# полного отчёта. Один вариант на юзера (кука cs_v2) применяется к ОБОИМ продуктам.
OBJECT_VARIANTS: dict[str, int] = {"A": PRICE_OBJECT, "B": PRICE_OBJECT, "C": PRICE_OBJECT}


def _object_price(variant: str) -> int:
    return OBJECT_VARIANTS.get(variant, OBJECT_VARIANTS["B"])


SELLER_ADDON_PRICE = 199


def _seller_addon_delta(variant: str) -> int:
    """Доплата за добавление продавца к object-отчёту.

    Была разницей цен (полная − объектная): при 599/349 выходило 250 ₽. С 05.08.2026
    цена фиксированная — разница потеряла смысл, когда цены перестали различаться по
    плечам, а считать её от текущих 499 и 299 дешевило бы апселл сильнее, чем мы хотим.
    Аргумент variant сохранён: его передают из СОХРАНЁННОГО заказа (не из живой куки),
    и он ещё пригодится, если вернём варьирование.
    """
    return SELLER_ADDON_PRICE


# ------------- helpers -------------

# Порог баланса NewDB. С 06.08.2026 оплату он больше НЕ блокирует (см.
# _provider_degraded) — остался только признаком «источник на нуле», по которому
# автоповтор решает, есть ли смысл дёргать платные базы прямо сейчас.
# 10 → 5 по решению владельца: полный отчёт стоит 3–5 ₽, то есть пятёрки хватает
# ровно на одну попытку. Ноль не ставим — при пустом балансе запрос упадёт и
# сожжёт попытку из трёх, ничего не проверив.
MIN_NEWDB_BALANCE = 5

# Порог по автомобильному поставщику. Один отчёт стоит ~32 ₽ (восемь вызовов),
# и при остатке меньше двух отчётов оплату мы всё равно пропускаем (см. ниже),
# но обязаны об этом кричать: на пустом apipoint деньги возьмутся, а собрать
# отчёт будет нечем. Это единственный расход, который реклама выжигает быстрее,
# чем приносит, — 14 000 ₽/нед трафика против 400 ₽ остатка.
MIN_APIPOINT_BALANCE = 64


def _provider_degraded() -> bool:
    """Платные базы недоступны или баланс на нуле. НЕ блокирует оплату.

    Раньше здесь стоял fail-closed гейт: не знаем состояние баз — не берём
    деньги, 503. Логика была верной ровно наполовину. Да, отчёт в этот момент не
    соберётся. Но источник моргает на минуты, а покупатель уходит навсегда: он
    дошёл до кнопки оплаты через шесть шагов мастера и второй раз не придёт.
    06.08 два таких отказа пришлись на вечерний пик, и это единственное, что мы
    в тот час потеряли безвозвратно.

    Отчёт мы доделываем ВСЕГДА — сбойные финализации теперь повторяются
    автоматически (_watch_stuck_finalizations), а раньше их доделывали руками.
    Поэтому правило перевёрнуто: деньги берём, отчёт довозим, а факт деградации
    помечаем в заказе и считаем метрикой.
    """
    # Смотрим НА СВОЕГО поставщика. Здесь стоял баланс NewDB — базы недвижимости,
    # к машинам отношения не имеющей: гейт исправно молчал, пока настоящий счёт,
    # с которого собирается каждый автоотчёт, подходил к нулю.
    try:
        from modules import apipoint
        bal = apipoint.last_balance()   # из журнала вызовов, бесплатно
    except Exception:  # noqa: BLE001 — импорт/окружение: считаем «не знаем»
        bal = None
    if bal is None or bal < MIN_APIPOINT_BALANCE:
        # Метрика прежняя: по ней видно, сколько оплат прошло на деградации и
        # сколько отчётов из-за этого поехали с задержкой.
        _bump_metric("provider_degraded_no_answer" if bal is None else "provider_degraded_low_balance")
        print(f"[ALERT] apipoint: остаток "
              f"{'неизвестен' if bal is None else f'{bal:.0f} ₽ (~{bal / 32:.0f} отчётов)'}"
              f" — оплату пропускаем, но ПОПОЛНИТЬ СРОЧНО: на нуле отчёт "
              f"собрать будет нечем", flush=True)
        return True
    return False


def _run_checks(req: "CheckRequest", preview: bool = False, object_only: bool = False,
                skip_object: bool = False, regioncode: str = "") -> list[Check]:
    """Оркестрация проверок по тарифу.

    preview=True — только объект (ЕГРН/Росреестр). Остальные базы (ФССП, банкротство,
    арбитраж, залоги) НЕ дёргаем, чтобы не тратить платные запросы на тех, кто не купит:
    полная проверка запускается после оплаты (_finalize_paid_report).
    base — ФССП (долги) по региону.
    ext (Полная) — паспорт→ИНН (действительность паспорта + ИНН), затем по ИНН
    банкротство и арбитраж, по ФИО — залоги; плюс ФССП. Всё параллельно.
    """
    # АВТОМОБИЛЬНАЯ ВЕТКА. Ниже по функции остался конвейер недвижимости от
    # ЧистойСделки — он не удалён намеренно: там отлажены платежи, ретраи и
    # гейты, и при желании вернуть проверку продавца по ФИО (ФССП, банкротство)
    # эта часть понадобится как есть. Для авто она не выполняется.
    from modules import auto as _auto
    if preview:
        return _auto.to_legacy_checks(_auto.preview_checks(req.object_ref))
    # Паспорт и владельцев кладём в ПОТОКОВОЕ хранилище, а не в модульную
    # переменную. Каждый отчёт финализируется в своём потоке, и общий глобал
    # означал бы, что два платежа в пределах десятка секунд перезапишут данные
    # друг друга — человек получил бы оплаченный отчёт с чужим VIN в паспорте.
    # Замок _finalizing от этого не спасает: он стережёт один report_id, а
    # конфликтуют РАЗНЫЕ.
    # Поштучные методы вместо агрегата: дешевле (≈32 ₽ против 50) и, главное,
    # ограничения, розыск и история приходят ЖИВЫМИ, а не снимком 2023 года.
    checks, extra = _auto.run_alacarte(req.object_ref, basic=object_only)
    # Паспорт и владельцы едут с теми же данными — прячем их в модуль-глобал,
    # чтобы не менять сигнатуру _run_checks, которую зовут из шести мест.
    _auto_extra.value = extra
    return _auto.to_legacy_checks(checks)

    from concurrent.futures import ThreadPoolExecutor
    from modules.fssp import region_from_address, region_from_kadastr
    from modules import premium

    from modules import rosreestr

    seller_name, seller_dob = req.seller_name, req.seller_dob
    full = req.tariff in ("ext", "full", "extended")

    checks: list[Check] = []
    inn = re.sub(r"\D", "", req.seller_inn or "")

    # ОБЪЕКТ: адрес → кадастр + проверка (обременения/доли) через Росреестр.
    # Заодно из объекта берём регион для ФССП (текстовый адрес → регион).
    # #12: skip_object — объект уже проверен (апселл продавца к object-отчёту), не платим за ЕГРН
    # повторно; регион берём из переданного regioncode или из кадастра.
    # Регион для ФССП: кадастр надёжнее всего, но если объект в ЕГРН не нашёлся,
    # берём субъект из текста адреса — иначе проверка продавца не запустится
    # вовсе, а человек за неё заплатил.
    if skip_object:
        regioncode = (regioncode or region_from_kadastr(req.object_ref)
                      or region_from_address(req.object_ref))
    else:
        obj = rosreestr.check_object(req.object_ref)
        regioncode = (obj.region or region_from_kadastr(req.object_ref)
                      or region_from_address(req.object_ref)
                      or region_from_address(getattr(obj, "address", "") or ""))
        checks.append(Check(key="object", name="Объект (ЕГРН / Росреестр)",
                            source="Росреестр", status=obj.status,
                            detail=obj.detail, items=obj.items,
                            permanent=getattr(obj, "permanent", False)))

    if preview:
        return checks  # превью: только объект по ЕГРН — платные базы не тратим до оплаты
    if object_only:
        return checks  # object-only отчёт: полный объект по ЕГРН, продавца НЕ проверяем (нет ФИО)

    # Полный тариф с паспортом → сначала получаем ИНН + проверяем паспорт.
    if full and req.passport_seria and req.passport_nomer:
        pas = premium.passport_and_inn(
            seller_name, seller_dob, req.passport_seria, req.passport_nomer
        )
        checks.append(Check(key="passport", name="Паспорт → ИНН (ФНС)",
                            source="ФНС", status=pas.status,
                            detail=pas.detail, items=pas.items))
        inn = inn or str(pas.extra.get("inn") or "")

    # Базовый тариф = объект + ФССП. Банкротство/арбитраж (по ИНН) — это
    # отдельная доп-услуга (апселл), в базовую проверку не входят.
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_fssp = ex.submit(check_enforcement, seller_name, seller_dob, regioncode)
        f_bank = ex.submit(check_bankruptcy, seller_name, seller_dob, inn,
                           300.0, req.object_ref or "") if full else None
        f_pld = ex.submit(premium.pledges, seller_name) if full else None
        fssp = f_fssp.result()
        bank = f_bank.result() if f_bank else None
        pld = f_pld.result() if f_pld else None

    checks.append(Check(key="enforcement", name="Исполнительные производства",
                        source="ФССП", status=fssp.status,
                        detail=fssp.detail, items=fssp.items))
    if full:
        checks.append(Check(key="bankruptcy", name="Банкротство продавца",
                            source="ЕФРСБ / Федресурс", status=bank.status,
                            detail=bank.detail, items=bank.items))
        checks.append(Check(key="pledges", name="Залоги и обременения",
                            source="Реестр залогов ФНП", status=pld.status,
                            detail=pld.detail, items=pld.items))
        arb = premium.arbitration(inn, timeout=150.0)
        checks.append(Check(key="arbitration", name="Арбитражные дела",
                            source="КАД Арбитр", status=arb.status,
                            detail=arb.detail, items=arb.items))
    return checks


def _serialize_check(c: Check) -> dict[str, Any]:
    return {
        "key": c.key,
        "name": c.name,
        "source": c.source,
        "status": c.status,
        "detail": c.detail,
        "items": c.items,
    }


# ------------- routes -------------

# ---------- A/B/n тест цены базового отчёта (раунд 2, 2026-07-13) ----------
# Раунд 1 (1490/490/990) выигран B=490 со значимостью (p=0.010/0.023), но с
# НДС 22% экономика при 490 — около нуля. Раунд 2 ищет revenue/визит по вилке:
#   A=299 (максимум конверсии), B=490 (контроль-победитель), C=690 (потолок?),
#   D=5299 ПРЕМИУМ — честно другой продукт: базовые проверки + банкротство и
#   арбитраж по ИНН ВКЛЮЧЕНЫ (без доплаты 249₽) + отдельный вывод юриста.
# Судим по ₽/визит на плечо, не по CVR (цены разные!). Счётчик обнулён.
# Полный отчёт (с продавцом) — A/B/C, раунд 3 (2026-07-24): 499/599/699. Раунд 2 (299/490/690)
# показал, что полный отчёт не покупают вообще (весь доход — с объектной проверки), D=5299 убран.
# Тот же вариант (кука cs_v2) применяется и к объектной цене (OBJECT_VARIANTS) — один тест на юзера.
# Цена одна для всех плеч (см. PRICE_FULL). Словарь оставлен: по нему живёт
# назначение варианта и вся отчётность — теперь плечи различаются не ценой, а видом.
# 05.08.2026 — раунд 4, ВИЗУАЛЬНЫЙ тест экрана цены. Цена у всех плеч одна,
# различается только то, как объясняется незавершённость проверки:
#   A — текущий экран (контроль): «всё готово, запускаем проверку»
#   B — «заключение уже создано», бланк ждёт открытия
#   C — «он знает — вы нет»: что известно продавцу и что известно покупателю
# Три плеча, а не четыре: на нашем трафике (~150 показов цены в день) даже три
# набирают значимость неделями, четвёртое растянуло бы тест ещё на треть.
# Судим по конверсии ОТ УВИДЕВШИХ ЦЕНУ (price_shown), а не от назначенных:
# до экрана цены доходит меньше половины, и деление на всех занижает результат
# в разы (на этом уже обожглись в раунде 3).
CS_VARIANTS: dict[str, int] = {"A": PRICE_FULL, "B": PRICE_FULL, "C": PRICE_FULL}


def _get_variant(request: Request) -> str:
    # Имя куки = версия раунда (cs_v2). Значение не из текущего набора (напр. отключённый
    # D или старый раунд) → "" → вызывающий откатывается на дефолт.
    v = request.cookies.get("cs_v2", "")
    return v if v in CS_VARIANTS else ""


def _detect_src(request: Request) -> str:
    """Источник визита: 'ad' (рекламный клик Директа) или 'organic'.
    Директ добавляет yclid к URL; плюс наши UTM (utm_medium=cpc / utm_source=yandex)."""
    q = request.query_params
    if q.get("yclid") or q.get("gclid"):
        return "ad"
    if (q.get("utm_medium") or "").lower() in ("cpc", "ppc", "paid"):
        return "ad"
    if (q.get("utm_source") or "").lower() in ("yandex", "direct"):
        return "ad"
    return "organic"


def _get_src(request: Request) -> str:
    """Липкий источник первого захода (кука cs_src)."""
    s = request.cookies.get("cs_src", "")
    return "ad" if s == "ad" else "organic"


# --- фильтр ботов (по User-Agent), чтобы краулеры/мониторинги не попадали в A/B-счётчики ---
_BOT_SIGS = (
    "crawler", "crawl", "spider", "slurp", "mediapartners", "googlebot", "yandexbot",
    "bingbot", "petalbot", "semrushbot", "ahrefsbot", "mj12bot", "dotbot", "duckduckbot",
    "baiduspider", "applebot", "facebookexternalhit", "telegrambot", "slackbot", "discordbot",
    "whatsapp", "vkshare", "skypeuripreview", "headless", "phantomjs", "puppeteer", "playwright",
    "selenium", "python-", "aiohttp", "okhttp", "go-http", "node-fetch", "axios/", "curl/",
    "wget", "libwww", "java/", "apache-httpclient", "httpclient", "scrapy", "postmanruntime",
    "monitor", "uptimerobot", "pingdom", "statuscake", "site24x7", "datadog",
)


def _is_bot(request: Request) -> bool:
    """Бот/краулер/скрипт по User-Agent (пустой UA тоже бот)."""
    ua = (request.headers.get("user-agent") or "").lower()
    if not ua:
        return True
    return any(sig in ua for sig in _BOT_SIGS)


def _assign_variant() -> str:
    import random
    return random.choice(list(CS_VARIANTS.keys()))


import re as _re
_CAMP_RE = _re.compile(r"[^\w .\-]", _re.UNICODE)
# ДД.ММ.ГГГГ или ISO — оба формата уже ходят по коду (маска ввода даёт первый).
_DOB_RE = _re.compile(r"^(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})$")


def _safe_camp(request: Request) -> str:
    """utm_campaign (или yandex_direct по yclid), очищенный по whitelist (защита от инъекций
    в дашборд/куку). Только буквы/цифры/пробел/точка/дефис, ≤60."""
    raw = (request.query_params.get("utm_campaign")
           or ("yandex_direct" if request.query_params.get("yclid") else ""))
    return _CAMP_RE.sub("", str(raw))[:60]


@app.get("/", response_class=HTMLResponse)
def landing(request: Request) -> HTMLResponse:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # #4/#7: SSR-цены варианта в первом рендере, чтобы показанная цена = списываемой ещё до
    # отработки /api/variant. Для НОВОГО посетителя назначаем вариант ЗДЕСЬ (до инжекта),
    # иначе рендерим дефолт B, а спишется случайно назначенный (флаш/недобор).
    existing = _get_variant(request)
    is_new = (not existing) and (not _is_bot(request))
    v0 = existing or (_assign_variant() if is_new else "B")
    price0 = CS_VARIANTS.get(v0, 599)
    pstr0 = f"{price0:,}".replace(",", " ") + " ₽"
    ostr0 = f"{_object_price(v0):,}".replace(",", " ") + " ₽"
    # legacy-литерал старой цены + class-спаны карточек. Подстановка идёт по ТЕКУЩЕМУ
    # литералу в разметке, поэтому при смене цены его надо менять и здесь — иначе
    # замена молча перестаёт срабатывать и на лендинге застывает прежняя цифра.
    html = (html.replace("1 490 ₽", pstr0).replace("от 1490 ₽", f"от {price0} ₽").replace("1490 ₽", pstr0)
                .replace('<span class="js-base-price">599 ₽</span>', f'<span class="js-base-price">{pstr0}</span>')
                .replace('<span class="js-object-price">299 ₽</span>', f'<span class="js-object-price">{ostr0}</span>'))
    resp = HTMLResponse(html)
    # Режим «не считать меня»: /?notrack=1 ставит куку на два года, /?notrack=0 снимает.
    # Нужен, чтобы владелец и разработчик могли ходить по бою, не ломая ровно те числа,
    # ради которых аналитика заводилась. Кука проверяется в трёх местах: приём событий
    # (модуль аналитики), счётчик назначений A/B ниже и пометка заказа как тестового.
    nt = (request.query_params.get("notrack") or "").strip()
    if nt in ("1", "on", "yes"):
        resp.set_cookie("cs_notrack", "1", max_age=60 * 60 * 24 * 730, samesite="lax")
        notrack = True
    elif nt in ("0", "off", "no"):
        resp.delete_cookie("cs_notrack")
        notrack = False
    else:
        notrack = bool(request.cookies.get("cs_notrack"))
    if is_new and not notrack:
        # Липкая кука на 90 дней — пользователь всегда видит ТОТ ЖЕ вариант, что отрендерили.
        v = v0
        src = _detect_src(request)  # рекламный клик или органика — по метке первого захода
        resp.set_cookie("cs_v2", v, max_age=60 * 60 * 24 * 90, samesite="lax")
        resp.set_cookie("cs_src", src, max_age=60 * 60 * 24 * 90, samesite="lax")
        camp = _safe_camp(request)  # атрибуция кампании (очищенная)
        if camp:
            resp.set_cookie("cs_camp", camp, max_age=60 * 60 * 24 * 90, samesite="lax")
        # Знаменатель CVR: счётчик назначений (раздельно реклама/органика).
        # Свои заходы сюда не идут — иначе конверсия падает от собственных проверок.
        abstats.bump_assignment(orders.PAYMENTS_LOG.parent, v, src, camp)
    return resp


@app.get("/api/variant")
def api_variant(request: Request) -> JSONResponse:
    v = _get_variant(request) or "B"
    price = CS_VARIANTS[v]
    obj_price = _object_price(v)
    return JSONResponse({
        "variant": v,
        "price": price,
        "object_price": obj_price,
        "object_price_str": (f"{obj_price:,}".replace(",", " ") + " ₽"),
        "price_str": f"{price:,}".replace(",", " ") + " ₽",
        # Гарантия возврата — константа (обещана на лендинге всем вариантам).
        "guarantee": True,
        # Цена расширенного отчёта приезжает с сервера, а не зашита числом в модалке:
        # она стояла в двух местах фронта, и подъём 199 → 299 показал бы человеку
        # одну сумму, а списал другую.
        "bump_price": BUMP_PRICE,
        "kit_price": DEALKIT_PRICE,
        "premium": False,  # D (премиум) отключён
    })


class ObjectCandidatesRequest(BaseModel):
    address: str = Field(..., min_length=6, max_length=300)


# Поиск объектов по адресу — ПЛАТНЫЙ (api-assist, 0,45-1,2 ₽/запрос), поэтому
# по умолчанию ВЫКЛЮЧЕН: без OBJECT_SEARCH_PAID=1 эндпоинт отвечает пустым
# списком, и фронт ведёт себя ровно как раньше (жёлтая подсказка «уточните
# адрес»). Включать после подключения тарифа.
def _object_search_enabled() -> bool:
    return os.getenv("OBJECT_SEARCH_PAID", "").strip() in ("1", "true", "yes")


# Сколько запросов суток НЕ отдаём превью. За сутки после оплаты уходит 15-30
# обращений к api-assist; берём с запасом, потому что цена ошибки несимметрична:
# не показали список квартир — человек введёт кадастр руками, а не доставили
# оплаченный отчёт — это возврат и письмо в поддержку.
_CAND_RESERVE = int(os.getenv("APIASSIST_RESERVE", "60") or 60)


# Номер квартиры из адреса, каким его пишет РЕЕСТР. Свою регулярку здесь держать
# нельзя: ЕГРН пишет и «кв. 3», и «дом 17, квартира 560», и «кв.№12». Узкий шаблон
# «кв\.?\s*\d» молча не видел московские адреса — у всех кандидатов номер выходил
# пустым, список подписывался «помещение», поиск по номеру не находил ничего, а
# автоподбор промахивался всегда. В modules/apiassist эта задача уже решена.
from modules.apiassist import _flat_of as _flat_of_address
# Хвост «, кв 127» отрезаем перед поиском по адресу: источник ищет по дому, а с
# номером квартиры в строке возвращает пусто (проверено на д. 58а — 0 против 70).
_FLAT_TAIL_RE = re.compile(r"[\s,;]*(?:кв|квартира|кв\.|пом|помещение|офис)\s*\.?\s*\d+[а-яa-z]?\s*$", re.I)


def _strip_flat(address: str) -> str:
    """Адрес без номера квартиры. Для поиска списка помещений дома."""
    return _FLAT_TAIL_RE.sub("", (address or "").strip()).strip(" ,;")


def _normalized_house(address: str) -> str:
    """Адрес в том виде, в каком его понял разбор — а не как набрал человек.

    Иначе списки врут: ввод «Каширское шоссе, д 58а, кв 3» без города разбирается
    как МОСКВА (индекс 115409), а поиск помещений по той же сырой строке домотал её
    до Домодедова и вернул чужой дом. Человек видел разбор одного адреса и квартиры
    из другого города. Нормализованная строка убирает расхождение: обе половины
    экрана говорят про один объект.
    """
    try:
        from modules import nspd
        clean = (nspd.clean_address(address) or {}).get("clean") or ""
    except Exception:  # noqa: BLE001
        clean = ""
    return clean or (address or "")


# Дом и корпус из адресной строки — в любом из написаний, которые встречаются в
# выдаче источника («д. 9», «дом 9», «д 31 к 1», «стр. 2»).
_HOUSE_NO_RE = re.compile(r"(?:^|[\s,])(?:дом|д|владение|вл)[\s.]*№?\s*(\d+[а-яa-z]?)", re.I)
_KORP_NO_RE = re.compile(r"(?:^|[\s,])(?:корпус|корп|стр(?:оение)?|к)[\s.]*№?\s*(\d+[а-яa-z]?)", re.I)


def _house_key(address: str) -> tuple[str, str]:
    """(дом, корпус) из адреса. Пустые строки — если не разобрали."""
    a = address or ""
    h = _HOUSE_NO_RE.search(a)
    k = _KORP_NO_RE.search(a)
    return ((h.group(1) or "").lower() if h else "",
            (k.group(1) or "").lower() if k else "")


def _same_house(row_addr: str, want: tuple[str, str]) -> bool:
    """Тот ли это дом. Источник ищет по адресу НЕЧЁТКО: на запрос «пр-кт Максима
    Горького, д. 9» он вернул д. 33, д. 3в, д. 25Б и д. 14 — то есть 94 строки из
    ста относились к чужим домам.

    Без этой проверки автоподбор мог найти «кв. 702» в соседнем доме, счесть её
    единственным совпадением и молча увести человека платить за проверку чужой
    квартиры. Показать лишнее в списке — неприятно, отдать отчёт не про тот
    объект — непоправимо.

    Корпус спрашиваем только если он назван во вводе: человек, написавший «д 31»
    без корпуса, имеет право увидеть оба, а неоднозначность отсечёт автоподбор.
    """
    wh, wk = want
    if not wh:                       # дом не разобрали — фильтровать нечем
        return True
    rh, rk = _house_key(row_addr)
    if rh != wh:
        return False
    return not wk or rk == wk


# Написания, до которых не дотягивается apiassist._FLAT_RE (там только кв/квартира/помещ*):
# сокращённое «пом. 5», комнаты общежитий и офисы.
_ROOM_RE = re.compile(r"(?:^|[\s,])(?:комната|комн|ком|помещение|пом|офис)[\s.]*№?\s*(\d+[а-яa-z]?)",
                      re.I)


def _flat_label(address: str, house: tuple[str, str] | None) -> str:
    """Чем строка подписана в списке выбора.

    Адресный поиск отдаёт ТОЛЬКО кадастр и адрес — ни площади, ни этажа. Значит
    номер помещения и есть единственное, по чему человек узнаёт своё; строка без
    него — пустая карточка, которую невозможно выбрать осмысленно.

    «к. 82» — ловушка: в «д 31 к 1» это корпус, а в общежитии на Максима Горького,
    9 — комната (корпусов с номером 910 не бывает). Различаем по цели поиска:
    когда во вводе корпус назван, все строки уже отфильтрованы по нему, и «к.» в
    них — тот самый корпус, а не комната.
    """
    flat = _flat_of_address(address)
    if flat:
        return flat
    m = _ROOM_RE.search(address or "")
    if m:
        return (m.group(1) or "").lower()
    if house and not house[1]:            # корпус во вводе не назван → «к.» = комната
        k = _KORP_NO_RE.search(address or "")
        if k:
            return (k.group(1) or "").lower()
    return ""


def _residential_only(rows: list[dict[str, Any]], limit: int = 400,
                      house: tuple[str, str] | None = None) -> list[dict[str, Any]]:
    """Оставить только жилые помещения: человек ищет свою квартиру, а поиск по
    адресу возвращает заодно здание целиком, подвалы и нежилые помещения —
    в списке выбора это шум, из-за которого выбор превращается в новую преграду.

    Лимит был 8 — при 66 помещениях в доме нужной там почти наверняка нет, и выбор
    превращался в тот же тупик, только оформленный. Отдаём все, фронт показывает
    список с прокруткой и поиском по номеру.
    """
    out = []
    dropped_house = 0
    dropped_blank = 0
    for r in rows or []:
        typ = str(r.get("type") or "").lower()
        purpose = str(r.get("purpose") or "").lower()
        if "здание" in typ or "сооружен" in typ or "участок" in typ:
            continue
        if house and not _same_house(str(r.get("address") or ""), house):
            dropped_house += 1
            continue
        if purpose and "жил" not in purpose:      # «нежилое» отсекаем
            continue
        if "нежил" in purpose:
            continue
        try:
            area = float(str(r.get("area") or 0).replace(",", "."))
        except Exception:  # noqa: BLE001
            area = 0.0
        if area > 400:                            # квартир такой площади не бывает
            continue
        cad = str(r.get("cad_number") or r.get("cadNumber") or "").strip()
        if not cad:
            continue
        addr = str(r.get("address") or "")
        # Номер квартиры — первое, что человек ищет глазами. Раньше в списке были
        # только площадь и этаж, и найти свою было нечем.
        label = _flat_label(addr, house)
        if not label:
            # Ни номера, ни площади, ни этажа (адресный поиск их не отдаёт) — выбрать
            # такую строку нельзя, а занимает она место наравне с остальными. Сюда же
            # попадает сам дом, когда источник возвращает его без типа «Здание».
            dropped_blank += 1
            continue
        out.append({"cad": cad, "area": area or None,
                    "floor": str(r.get("floor") or "") or None,
                    "address": addr,
                    "flat": label,
                    "purpose": str(r.get("purpose") or "")})
        if len(out) >= limit:
            break
    # По номеру квартиры, а не по порядку выдачи источника: список читают как
    # подъездную табличку.
    out.sort(key=lambda c: (int(re.sub(r"\D", "", c["flat"]) or 10**9), c["flat"]))
    if dropped_house or dropped_blank:
        print(f"[flats] отсев: чужих домов {dropped_house}, без номера {dropped_blank}; "
              f"осталось {len(out)}", flush=True)
    return out


def _looks_multiflat(obj: dict[str, Any]) -> bool:
    """Многоквартирный ли это дом. Нужно, чтобы не спрашивать «выберите квартиру»
    у того, кто покупает частный дом целиком.

    Только бесплатные признаки — превью не имеет права ждать платный запрос.
    НСПД пишет назначение прямым текстом: «Жилой дом» против «Многоквартирного».
    У Бойчука дом 84 м² и назначение «Жилой дом», у дома Чижовой — 6 213 м² и
    «Многоквартирный».
    """
    if "здани" not in str(obj.get("type") or "").lower():
        return False
    purpose = str(obj.get("purpose") or "").lower()
    if "многоквартир" in purpose:
        return True
    if purpose:                       # назначение известно и оно не МКД — не трогаем
        return False
    # Назначения нет (так отвечает запасной источник) — решаем по площади. Считать
    # квартиры здесь НЕЛЬЗЯ: это платный запрос на 45 секунд ВНУТРИ превью, и оно
    # повисает. Список фронт запрашивает отдельно, уже после отрисовки, и если
    # квартир меньше двух — просто ничего не покажет.
    try:
        area = float(str(obj.get("area") or 0).replace(",", "."))
    except Exception:  # noqa: BLE001
        area = 0.0
    return area >= 300                # частный дом такой площади не бывает


def _house_flats(address: str) -> list[dict[str, Any]]:
    """Жилые помещения дома по адресу. Пустой список — если выключено, лимит
    исчерпан или источник не ответил."""
    if not _object_search_enabled():
        return []
    try:
        from modules import apiassist
        if not apiassist.budget_ok(reserve=_CAND_RESERVE):
            return []
        house = _strip_flat(_normalized_house(address))
        # 20 с, а не 45: источник отвечает за 2-3 секунды, а этот вызов стоит ВНУТРИ
        # превью — щедрый таймаут превращает заминку источника в зависший экран.
        rows = _residential_only(apiassist.search_by_address(house, timeout=20),
                                 house=_house_key(house))
        try:
            with open(orders.PAYMENTS_LOG.parent / "apiassist.jsonl", "a", encoding="utf-8") as _f:
                _f.write(json.dumps({"ts": datetime.utcnow().isoformat() + "Z",
                                     "event": "house_search", "found": len(rows)},
                                    ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
        return rows
    except Exception as e:  # noqa: BLE001 — подсказка не имеет права ломать превью
        print(f"[flats] {type(e).__name__}: {e}", flush=True)
        return []


def _autopick_flat(address: str, flat_no: str) -> str:
    """Номер квартиры человек уже назвал во вводе — найдём её в списке помещений
    дома сами, без вопросов. Возвращает кадастровый номер или пусто.

    Спрашивать «выберите свою квартиру» у того, кто её номер только что написал, —
    лишний шаг, на котором часть людей отваливается. Список показываем только когда
    совпадения нет.
    """
    if not flat_no or not _object_search_enabled():
        return ""
    # Ищем по ДОМУ, а не по полному адресу: с «кв 127» в строке источник стабильно
    # отдаёт ноль записей, тогда как по тому же дому без квартиры — семьдесят.
    rows = _house_flats(address)
    want = flat_no.strip().lower()
    hit = [c for c in rows if c.get("flat", "").strip().lower() == want]
    # Ровно одно совпадение — берём. Два и больше (бывает при слитых адресах
    # соседних домов) означают неоднозначность: тут честнее спросить.
    if len(hit) == 1:
        print(f"[autopick] кв. {flat_no} → {hit[0]['cad']}", flush=True)
        _bump_metric("autopick_hit")
        return hit[0]["cad"]
    _bump_metric("autopick_miss")
    return ""


@app.post("/api/object-candidates")
def api_object_candidates(req: ObjectCandidatesRequest, request: Request) -> JSONResponse:
    """Список квартир по адресу, когда точный кадастр определить не удалось.

    Человек выбирает свою по номеру. Пустой список = фронт показывает прежнюю
    подсказку.

    Список собирает _house_flats — та же функция, что и автоподбор. Раньше здесь
    был свой, параллельный вызов источника, и когда автоподбор научился отсеивать
    чужие дома, этот путь остался без проверки: на «пр-кт Максима Горького, д 9»
    экран предлагал «кв. 9» из д. 33, д. 3в и д. 25Б.
    """
    if not _object_search_enabled():
        return JSONResponse({"candidates": [], "enabled": False})
    if not _rate_ok("cand:" + _client_ip(request), limit=10, window=3600):
        return JSONResponse({"candidates": [], "enabled": True})
    try:
        from modules import apiassist
        # Суточный лимит тарифа делим не поровну: хвост принадлежит тем, кто уже
        # заплатил. Превью — приятная помощь, фолбэк после оплаты — обязательство.
        if not apiassist.budget_ok(reserve=_CAND_RESERVE):
            _bump_metric("cand_budget_stop")
            print(f"[cand] дневной лимит на исходе ({apiassist.used_today()}/"
                  f"{apiassist.day_limit()}) — превью не тратим, платный путь цел", flush=True)
            return JSONResponse({"candidates": [], "enabled": True})
        cands = _house_flats(req.address)
        print(f"[cand] {req.address[:48]!r}: в списке {len(cands)}", flush=True)
        return JSONResponse({"candidates": cands, "enabled": True})
    except Exception as e:  # noqa: BLE001 — подсказка не имеет права ломать превью
        print(f"[cand] ошибка: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"candidates": [], "enabled": True})


@app.get("/favicon.svg")
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/admin/payments")
def admin_payments(token: str = "", format: str = "csv") -> Any:
    """Выгрузка реестра оплат (email, сумма, тариф, дата). Только по ADMIN_TOKEN."""
    from fastapi.responses import Response
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="forbidden")
    rows = orders.read_payments()
    if format == "json":
        total = sum(int(r.get("amount") or 0) for r in rows)
        return JSONResponse({"count": len(rows), "revenue_rub": total, "payments": rows})
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["paid_at", "email", "amount", "variant", "tariff", "order_id", "payment_id", "status"])
    for r in rows:
        w.writerow([r.get("paid_at", ""), r.get("email", ""), r.get("amount", ""),
                    r.get("variant", ""), r.get("tariff", ""), r.get("order_id", ""),
                    r.get("payment_id", ""), r.get("status", "")])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=payments.csv"})


def _require_admin(token: str) -> None:
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="forbidden")


@app.get("/admin/stuck", response_class=HTMLResponse)
def admin_stuck(token: str = "") -> Any:
    """Оплачено, но отчёт не выдан. Автоматика такие НЕ перезапускает — только
    показывает; платный перезапуск инициирует человек кнопкой ниже."""
    _require_admin(token)
    from html import escape
    rows = _find_stuck_paid()
    try:
        from modules import newdb
        bal = newdb.balance()
    except Exception:  # noqa: BLE001
        bal = None

    if not rows:
        body = ('<p class="ok">Все оплаченные отчёты выданы.</p>')
    else:
        # Какие объекты уже оплачены и забраны даром — их перезапуск ничего не стоит.
        try:
            from modules import newdb as _nd
            free_refs = {str(v.get("ref", "")).strip().lower()
                         for v in _nd._load(_nd._RECOVERED_FILE).values() if v.get("ref")}
        except Exception:  # noqa: BLE001
            free_refs = set()

        trs = []
        for s in rows:
            hopeless = s.get("hopeless")
            is_free = s.get("object_ref", "").strip().lower() in free_refs
            note = ('<span class="bad">объекта нет в ЕГРН — перезапуск не поможет, '
                    'нужен возврат</span>' if hopeless else
                    ('<span class="ok">ответ уже оплачен и забран — '
                     'перезапуск БЕСПЛАТНЫЙ</span>' if is_free else ""))
            label = "Перезапустить даром" if is_free else "Перезапустить"
            btn = ("" if hopeless else
                   f'<form method="post" action="/admin/stuck/run">'
                   f'<input type="hidden" name="token" value="{escape(token)}">'
                   f'<input type="hidden" name="report_id" value="{escape(s["report_id"])}">'
                   f'<button type="submit">{label}</button></form>')
            trs.append(
                f'<tr><td><code>{escape(s["report_id"])}</code></td>'
                f'<td class="num">{s["amount"]} ₽</td>'
                f'<td>{escape(s["tariff"])}</td>'
                f'<td>{escape(s["email"])}</td>'
                f'<td>{escape(s["object_ref"][:60])}<br>{note}</td>'
                f'<td class="num">{s["attempts"]}</td>'
                f'<td>{escape(str(s["paid_at"])[:16])}</td>'
                f'<td>{btn}</td></tr>')
        body = ('<table><thead><tr><th>Отчёт</th><th>Сумма</th><th>Тариф</th>'
                '<th>Клиент</th><th>Объект</th><th>Попыток</th><th>Оплачен</th>'
                '<th></th></tr></thead><tbody>' + "".join(trs) + '</tbody></table>')

    bal_txt = (f"баланс NewDB: <b>{bal} ₽</b>" if bal is not None
               else "баланс NewDB: <b>не отвечает</b>")
    try:
        from modules import newdb as _nd2
        ps = _nd2.pending_stats()
        pend_txt = (
            f'<p class="warn" style="background:#eef6ff;border-color:#c8dcff">'
            f'Оплачено, ответ ещё не доехал: <b>{ps["waiting"]}</b>'
            + (f' (самому старому {ps["oldest_min"]} мин)' if ps["waiting"] else "")
            + f' · уже забрано даром: <b>{ps["recovered"]}</b>. '
            f'Сборщик опрашивает их каждые {_SWEEP_PERIOD_SEC // 60} мин — опрос '
            f'по requestId не тарифицируется. Как только ответ заберётся, '
            f'перезапуск такого отчёта станет бесплатным.</p>')
    except Exception:  # noqa: BLE001
        pend_txt = ""
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
<title>Застрявшие оплаты</title>
<style>
 body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:32px;color:#111}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#666;margin:0 0 20px}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border-bottom:1px solid #e6e6e6;padding:9px 10px;text-align:left;vertical-align:top}}
 th{{font-weight:600;color:#555;background:#fafafa}}
 .num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
 code{{font-size:12px}} .ok{{color:#0a7d34}} .bad{{color:#b3261e;font-size:12px}}
 button{{padding:6px 12px;border:1px solid #1b5cff;background:#1b5cff;color:#fff;
        border-radius:6px;cursor:pointer;font-size:13px}}
 button:hover{{background:#0f47d6}}
 .warn{{background:#fff8e1;border:1px solid #ffe1a3;padding:10px 12px;
        border-radius:8px;margin:0 0 18px;font-size:13px}}
</style>
<h1>Оплачено, отчёт не выдан</h1>
<p class="sub">{bal_txt} · записей: {len(rows)}</p>
<p class="warn">Автоматический перезапуск отключён намеренно: он не отличал
«сервис не ответил» от «объекта нет в ЕГРН» и 25.07.2026 сжёг баланс на
132 повторах одного адреса. Каждый запуск ниже — платный, решение за человеком.</p>
{pend_txt}
{body}""")


@app.post("/admin/stuck/run")
def admin_stuck_run(token: str = Form(""), report_id: str = Form("")) -> Any:
    """Ручной перезапуск финализации ОДНОГО отчёта. Платно — поэтому только отсюда."""
    _require_admin(token)
    from fastapi.responses import RedirectResponse
    rid = (report_id or "").strip()
    target = next((s for s in _find_stuck_paid() if s["report_id"] == rid), None)
    if not target:
        raise HTTPException(status_code=404, detail="не найден среди застрявших")
    if target.get("hopeless"):
        raise HTTPException(status_code=409, detail="объекта нет в ЕГРН — нужен возврат")
    r = _load_report(rid) or {}
    r["finalize_attempts"] = int(r.get("finalize_attempts") or 0) + 1
    _save_report_record(r)
    print(f"[stuck] РУЧНОЙ перезапуск report={rid} tariff={target['tariff']}", flush=True)
    import threading
    threading.Thread(
        target=_finalize_then_notify,
        args=(rid, target.get("email") or "", target["tariff"] == "object"),
        daemon=True).start()
    return RedirectResponse(f"/admin/stuck?token={token}", status_code=303)


@app.get("/admin/ab-stats")
def admin_ab_stats(token: str = "", format: str = "html", src: str = "all") -> Any:
    """A/B-статистика теперь ЧАСТЬ единого дашборда /admin/stats (панель «A/B-тест цены»).
    HTML-запрос редиректит туда; format=json оставлен для обратной совместимости (скрипты)."""
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="forbidden")
    src = src if src in ("all", "ad", "organic") else "all"
    if format == "json":
        ledger = orders.PAYMENTS_LOG.parent
        counts, since = abstats.read_assignments(ledger, src)
        stats = abstats.build_stats(list(CS_VARIANTS.keys()), counts, since, orders.read_payments(), src)
        return JSONResponse(stats)
    return RedirectResponse(f"/admin/stats?token={token}&src={src}", status_code=307)


# /api/goal + /admin/stats — обслуживает вендор-модуль analytics (см. ANALYTICS_CFG выше).
# A/B-тест, тарифы, вовлечённость — панели того же дашборда (_an_panels / _an_extra_kpis).


def _legal_page(name: str) -> HTMLResponse:
    path = STATIC_DIR / "legal" / f"{name}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page() -> HTMLResponse:
    return _legal_page("privacy")


@app.get("/consent", response_class=HTMLResponse)
def consent_page() -> HTMLResponse:
    return _legal_page("consent")


@app.get("/offer", response_class=HTMLResponse)
def offer_page() -> HTMLResponse:
    return _legal_page("offer")


@app.get("/api/tariffs")
def get_tariffs() -> dict[str, Any]:
    return {"tariffs": TARIFFS}


@app.get("/api/suggest-address")
def api_suggest_address(request: Request, q: str = "") -> JSONResponse:
    """Подсказки адреса (DaData suggestions) — проксируем, ключ не светим в HTML."""
    if not _rate_ok("suggest:" + _client_ip(request), limit=120, window=3600):
        return JSONResponse({"suggestions": []})
    key = os.getenv("DADATA_API_KEY", "").strip()
    q = (q or "").strip()
    if not key or len(q) < 3:
        return JSONResponse({"suggestions": []})
    try:
        import httpx
        r = httpx.post(
            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Token {key}"},
            json={"query": q, "count": 6}, timeout=8.0,
        )
        data = r.json()
        out = [{"value": s.get("value")} for s in data.get("suggestions", []) if s.get("value")]
        return JSONResponse({"suggestions": out})
    except Exception:
        return JSONResponse({"suggestions": []})


@app.get("/api/healthz")
def healthz() -> dict[str, Any]:
    # Публично — только факт живости. Детали интеграций (какие ключи настроены) наружу не
    # раскрываем (разведка для атакующего); они доступны в /admin/selfcheck под токеном.
    return {"ok": True}


# Проверки идут во внешние async-API (~50-60с суммарно) — дольше, чем
# держит соединение прокси (60с). Поэтому /api/check запускает работу в
# ФОНЕ и сразу отдаёт job_id; фронт опрашивает /api/check/status/{id}.
JOBS: dict[str, dict[str, Any]] = {}


def _maybe_alert_low_balance() -> None:
    """Низкий баланс NewDB → предупредить владельца в Telegram (не чаще раза в 6ч).
    Вызывается после каждой проверки: если денег мало, проверки скоро начнут
    падать и отчёты выйдут пустыми — предупреждаем заранее. Тихо глушит ошибки."""
    try:
        chat = os.getenv("OWNER_TG_CHAT_ID", "").strip()
        if not chat:
            return
        # 50 ₽ по решению владельца 27.07.2026: при цене проверки ~1 ₽ порог 500
        # срабатывал за сотни проверок до реальной проблемы и читался как шум.
        threshold = int(os.getenv("NEWDB_MIN_BALANCE", "50") or "50")
        from modules import newdb, monitoring
        bal = newdb.balance()
        if bal is None or bal >= threshold:
            return
        import time as _t
        ledger = orders.PAYMENTS_LOG.parent
        now = int(_t.time())
        if now - abstats.read_counter(ledger, "balance_alert_ts") < 6 * 3600:
            return
        monitoring.send_telegram(
            chat,
            f"⚠️ ЧистаяСделка: баланс NewDB низкий — {bal} ₽ (порог {threshold}).\n"
            f"Скоро проверки ЕГРН/ФССП начнут падать, а отчёты выходить пустыми. "
            f"Пополните баланс: https://newdb.net",
        )
        abstats.set_counter(ledger, "balance_alert_ts", now)
    except Exception:
        pass


# Кадастровые округа новых территорий (приказ Росреестра П/0490 от 14.12.2022).
# Сведений ЕГРН по ним в открытой выдаче нет: реестр там ещё наполняется, и ни
# НСПД, ни api-assist, ни NewDB не отдают по этим номерам ничего — проверено на
# восьми номерах всех четырёх округов, ноль ответов. Проверить объект мы не можем,
# и человек должен узнать об этом ДО оплаты, а не после (заказ 78eab183392d: 798 ₽
# за объект 95:19:0101049:342, который не нашёлся бы никогда).
# Названия в ПРЕДЛОЖНОМ падеже: значение подставляется только в оборот
# «в <…> кадастровом округе», и именительный давал «в Луганский округе».
_NO_EGRN_DISTRICTS = {"93": "Донецком", "94": "Херсонском",
                      "95": "Луганском", "96": "Запорожском"}
# Коды КЛАДР тех же регионов — по ним ловим ввод АДРЕСОМ. Внимание: код КЛАДР и
# кадастровый округ у одного региона разные (ЛНР — КЛАДР 94, округ 95), поэтому
# таблицы две, а не одна. Со старыми регионами пересечений нет: субъектов 89,
# Крым 91, Севастополь 92.
_NO_EGRN_REGION_CODES = {"90": "Запорожском", "93": "Донецком",
                         "94": "Луганском", "95": "Херсонском"}


# Названия регионов прямо в тексте адреса — на случай, когда DaData молчит
# (кончился дневной лимит стандартизации, и тогда ни кадастра, ни кода региона
# у нас нет). Ловим только сами регионы: «Донецк» отдельным словом брать нельзя —
# город с таким названием есть и в Ростовской области.
_NO_EGRN_ADDR_RE = [
    (re.compile(r"луганск\w*\s+народн|\bлнр\b", re.I), "Луганском"),
    (re.compile(r"донецк\w*\s+народн|\bднр\b", re.I), "Донецком"),
    (re.compile(r"херсонск\w*\s+(обл|область)", re.I), "Херсонском"),
    (re.compile(r"запорожск\w*\s+(обл|область)", re.I), "Запорожском"),
]


def _no_egrn_district(object_ref: str, region_code: str = "", cad: str = "") -> str:
    """Название кадастрового округа, по которому ЕГРН недоступен ('' — обычный регион).

    Смотрим на что угодно, что уже знаем об объекте: введённый кадастр, кадастр,
    подобранный DaData по адресу, код региона из того же ответа и — как последний
    рубеж — название региона в самом тексте адреса.
    """
    for value in (object_ref, cad):
        head = str(value or "").strip()[:2]
        if head in _NO_EGRN_DISTRICTS and ":" in str(value or ""):
            return _NO_EGRN_DISTRICTS[head]
    by_code = _NO_EGRN_REGION_CODES.get(str(region_code or "").strip(), "")
    if by_code:
        return by_code
    text = str(object_ref or "")
    for rx, name in _NO_EGRN_ADDR_RE:
        if rx.search(text):
            return name
    return ""


# Список блоков берём из auto — один источник правды и для превью, и для
# платного отчёта. Разъехавшись, они дали бы классику: на экране оплаты обещаны
# одни проверки, в отчёте приходят другие.
from modules.auto import AUTO_CHECKS as _PREVIEW_BASES  # noqa: E402


def _run_job(job_id: str, req: "CheckRequest", ym_uid: str = "") -> None:
    try:
        report_id = uuid.uuid4().hex[:12]
        # ПРЕВЬЮ: НЕ дёргаем ни одной платной базы — не тратим запросы на тех, кто не купит.
        # Показываем список баз со статусом «после оплаты». Полная проверка — после оплаты.
        checks = [Check(key=k, name=n, source=s, status="pending",
                        detail="Проверяется в полном отчёте после оплаты", items=[])
                  for k, n, s in _PREVIEW_BASES]
        # Данные объекта в превью (реальный вес до оплаты):
        #  - кадастр → бесплатно из НСПД;
        #  - адрес → DaData Clean (~0.2₽/запрос, первые 100 бесплатно) даёт кадастр квартиры,
        #    затем бесплатно НСПД. NewDB (дорогие базы) НЕ трогаем до оплаты.
        obj_preview = None
        parsed_addr: list = []
        want_flat = ""
        house_only = False
        no_egrn = ""

        # АВТОМОБИЛЬНОЕ ПРЕВЬЮ. Опознание по VIN стоит 1,10 ₽ — это единственный
        # платный вызов до оплаты, и он окупается: человек должен убедиться, что
        # нашли ИМЕННО его машину, иначе он не заплатит. По госномеру вдвое
        # дороже (нужна конвертация в VIN), поэтому в форме просим VIN.
        source_down = False
        try:
            from modules import auto as _auto
            _pv = _auto.preview(req.object_ref)
            # Источник молчит или у нас кончился баланс — платный отчёт тоже не
            # пройдёт. Значит и деньги брать нельзя: это ровно тот случай, когда
            # человек платит и не получает ничего. Кнопку оплаты гасит фронт.
            source_down = str(_pv.get("why") or "").startswith(("no_funds", "source_error"))
            if _pv.get("found"):
                obj_preview = {
                    "vin": _pv.get("vin"), "kind": _pv.get("kind"),
                    "marka": _pv.get("marka"), "model": _pv.get("model"),
                    "year": _pv.get("year"), "engine": _pv.get("engine"),
                    "body": _pv.get("body"),
                }
            else:
                # Причину не прячем: «не распознали ввод» и «источник молчит» —
                # разные проблемы, и человеку нужно понимать, что делать дальше.
                print(f"[preview] машина не опознана: {_pv.get('why')} "
                      f"ref={str(req.object_ref)[:24]!r}", flush=True)
        except Exception as e:  # noqa: BLE001 — превью не имеет права ронять заявку
            print(f"[preview] {type(e).__name__}: {e}", flush=True)
            source_down = True

        # Метрика качества опознания: сколько введённых VIN/госномеров вообще
        # находятся в базе. Если доля низкая — проблема во вводе или в источнике,
        # и это видно раньше, чем по жалобам.
        try:
            _ledger = orders.PAYMENTS_LOG.parent
            abstats.bump_counter(_ledger, "autolookup_total")
            if obj_preview:
                abstats.bump_counter(_ledger, "autolookup_found")
            with open(_ledger / "autolookup.jsonl", "a", encoding="utf-8") as _f:
                _f.write(json.dumps({
                    "ts": datetime.utcnow().isoformat() + "Z",
                    "found": bool(obj_preview),
                    "marka": (obj_preview or {}).get("marka") or "",
                    "year": (obj_preview or {}).get("year") or "",
                }, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass
        head = "Предварительная проверка — итоговый риск в полном отчёте"
        record = {
            "id": report_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "seller_name": req.seller_name, "seller_dob": req.seller_dob,
            "object_ref": req.object_ref, "seller_inn": req.seller_inn,
            "email": req.email, "tariff": req.tariff, "ym_uid": ym_uid,
            "risk": "preview", "headline": head, "body": "",
            "recommendations": [], "llm_used": False, "preview": True,
            "object_preview": obj_preview,
            # Разобранный адрес и «это дом, а не квартира» нужны фронту, чтобы вместо
            # пустого места показать хоть что-то осмысленное. Стоят ноль запросов.
            "parsed_address": parsed_addr,
            "want_flat": want_flat,
            "house_only": house_only,
            # Округ, по которому ЕГРН недоступен ('' — обычный регион).
            "no_egrn_district": no_egrn,
            "checks": [_serialize_check(c) for c in checks],
            "pdf_path": str(REPORTS_DIR / f"{report_id}.pdf"),
        }
        REPORTS[report_id] = record
        try:
            (REPORTS_DIR / f"{report_id}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        except Exception:
            pass
        # Машину не опознали — фронт покажет подсказку («проверьте VIN, 17 знаков»),
        # а не пустой экран с кнопкой «оплатить». Платить за проверку неизвестно
        # чего человек не должен: это прямой путь к возврату.
        addr_unresolved = obj_preview is None
        show_candidates = False
        flat_unknown = False
        JOBS[job_id] = {"status": "done", "result": {
            "report_id": report_id, "risk": "preview", "headline": head,
            "body": "", "recommendations": [], "llm_used": False,
            "object_preview": obj_preview, "addr_unresolved": addr_unresolved,
            "source_down": source_down,
            "parsed_address": parsed_addr, "want_flat": want_flat, "house_only": house_only,
            "show_candidates": show_candidates, "flat_unknown": flat_unknown,
            "no_egrn_district": no_egrn,
            "checks": record["checks"], "report_url": f"/api/report/{report_id}",
        }}
        _maybe_alert_low_balance()
    except Exception as e:  # noqa: BLE001 — фон, ошибку отдаём в статус
        JOBS[job_id] = {"status": "error", "detail": f"{type(e).__name__}: {e}"}


@app.post("/api/check")
def api_check(req: CheckRequest, request: Request) -> JSONResponse:
    # Чиним опечатку в кадастровом номере ДО всех запросов: иначе «50 :30 :0050117 :457»
    # уходит в поиск как адрес, ничего не находит, и человек упирается в тупик
    # «не нашли квартиру» — платить ему не за что, конверсия в этой ветке 1% против 6%.
    try:
        from modules.rosreestr import normalize_cadastre
        fixed = normalize_cadastre(req.object_ref)
        if fixed != (req.object_ref or "").strip():
            print(f"[norm] кадастр поправлен: {req.object_ref!r} -> {fixed!r}", flush=True)
            req.object_ref = fixed
    except Exception as e:  # noqa: BLE001 — нормализация не имеет права ломать заявку
        print(f"[norm] пропущено: {type(e).__name__}: {e}", flush=True)
    # Пропущенное двоеточие («86:130501002:333»). Разбить можно двумя способами,
    # поэтому вариант принимаем ТОЛЬКО если реестр подтвердил, что объект есть —
    # иначе подставили бы человеку чужую недвижимость. НСПД бесплатен, проверка
    # идёт лишь для заведомо битого ввода, так что лишних трат нет.
    try:
        from modules.rosreestr import glued_cadastre_variants
        from modules import nspd as _nspd_fix
        for cand in glued_cadastre_variants(req.object_ref):
            if _nspd_fix.object_by_cadastre(cand):
                print(f"[norm] слипшийся кадастр: {req.object_ref!r} -> {cand!r} "
                      f"(подтверждён в реестре)", flush=True)
                req.object_ref = cand
                break
    except Exception as e:  # noqa: BLE001
        print(f"[norm] склейка пропущена: {type(e).__name__}: {e}", flush=True)
    # Согласие на ПДн нужно ТОЛЬКО если переданы персональные данные продавца. Бесплатное
    # превью по объекту (без ФИО/ДР) обрабатывает данные квартиры, а не человека — согласие
    # не требуется (и не собираем чужие ПДн у всех подряд «на всякий случай»).
    if req.seller_name and not req.consent:
        raise HTTPException(
            status_code=400,
            detail="Требуется согласие на обработку персональных данных (152-ФЗ).",
        )
    # Антибот: бесплатная проверка ходит в платные API (~2-10₽) — лимитируем
    # по IP (реальному пользователю 6/час хватит; бота-скрейпер отсекает).
    # Порог вынесен в окружение ТОЛЬКО ради локальной отладки: на проде переменной
    # нет, и остаётся прежняя шестёрка. Поднимать её в бою нельзя — платит DaData.
    ip = _client_ip(request)
    try:
        _lim = int(os.getenv("CHECK_RATE_LIMIT", "6") or 6)
    except ValueError:
        _lim = 6
    if not _rate_ok("check:" + ip, limit=_lim, window=3600):
        raise HTTPException(
            status_code=429,
            detail="Слишком много бесплатных проверок с вашего адреса. "
                   "Попробуйте через час или напишите нам, если это ошибка.",
        )
    import threading
    job_id = uuid.uuid4().hex[:16]
    JOBS[job_id] = {"status": "processing"}
    ym_uid = request.cookies.get("_ym_uid", "")  # ClientId Метрики для offline-конверсии
    threading.Thread(target=_run_job, args=(job_id, req, ym_uid), daemon=True).start()
    return JSONResponse({"job_id": job_id, "status": "processing"})


def _preview_result(result: dict[str, Any]) -> dict[str, Any]:
    """Бесплатное превью: уровень риска + статусы проверок, но БЕЗ деталей,
    рекомендаций и PDF. Полное содержимое — после оплаты (freemium)."""
    checks = []
    for c in result.get("checks", []):
        checks.append({
            "key": c.get("key"), "name": c.get("name"), "source": c.get("source"),
            "status": c.get("status"),
            "detail": "🔒 Детали и рекомендации — в полном отчёте.",
            "items": [], "locked": True,
        })
    n_flags = sum(1 for c in result.get("checks", []) if c.get("status") == "found")
    n_checked = sum(1 for c in result.get("checks", []) if c.get("status") != "not_checked")
    if n_checked == 0:
        body = ("Не удалось выполнить ни одной проверки по этим данным. "
                "Проверьте VIN — в нём ровно 17 знаков, без букв О, Ч и Ы, — "
                "или укажите госномер, и запустите снова.")
    else:
        body = (f"Машину проверим по {n_checked} "
                f"{'базе' if n_checked == 1 else 'базам'}: залог, "
                "ограничения ГИБДД, розыск, ДТП, пробег, такси, таможня. "
                "Детали по каждой, дату актуальности данных и PDF-отчёт "
                "для сделки — откроются после оплаты.")
    return {
        "report_id": result.get("report_id"),
        "risk": result.get("risk"),
        "headline": "Проверка завершена — доступно превью",
        "body": body,
        "recommendations": [],
        "checks": checks,
        "paid": False,
        "n_flags": n_flags,
        "object_preview": result.get("object_preview"),
        "addr_unresolved": result.get("addr_unresolved"),  # адрес не найден → фронт покажет подсказку
        # Разобранный адрес показываем вместо пустого места: «регион, район, посёлок,
        # улица, индекс» подтверждает человеку, что мы его поняли, и стоит ноль запросов.
        "parsed_address": result.get("parsed_address") or [],
        "want_flat": result.get("want_flat") or "",
        "house_only": bool(result.get("house_only")),
        "show_candidates": bool(result.get("show_candidates")),
        # Список полей здесь ЗАКРЫТЫЙ — что не перечислено, до фронта не доедет.
        # На этом уже споткнулись: флаг посчитали в превью, а экран его не увидел.
        "flat_unknown": bool(result.get("flat_unknown")),
        # Новые территории: объект не проверим ничем. Фронт по этому полю говорит
        # об этом ДО оплаты и прячет тариф «только объект».
        "no_egrn_district": result.get("no_egrn_district") or "",
        "report_url": None,
    }


@app.get("/api/check/status/{job_id}")
def api_check_status(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job["status"] == "done":
        result = job["result"]
        if orders.is_report_paid(result.get("report_id", "")):
            return JSONResponse({"status": "done", "paid": True, **result})
        return JSONResponse({"status": "done", **_preview_result(result)})
    if job["status"] == "error":
        return JSONResponse({"status": "error", "detail": job.get("detail", "ошибка")})
    return JSONResponse({"status": "processing"})


def _load_report(report_id: str) -> dict[str, Any] | None:
    rec = REPORTS.get(report_id)
    if rec:
        return rec
    j = REPORTS_DIR / f"{report_id}.json"
    if j.exists():
        try:
            REPORTS[report_id] = json.loads(j.read_text(encoding="utf-8"))
            return REPORTS[report_id]
        except Exception:
            return None
    return None


@app.get("/api/check/full/{report_id}")
def api_check_full(report_id: str) -> JSONResponse:
    """Полный отчёт — только если он оплачен (используется после возврата
    с оплаты, чтобы разблокировать детали на странице результата)."""
    rec = _load_report(report_id)
    if not rec:
        raise HTTPException(status_code=404, detail="report not found")
    if not orders.is_report_paid(report_id):
        raise HTTPException(status_code=402, detail="Отчёт доступен после оплаты.")
    if rec.get("preview"):
        # Оплачено, но полная проверка ещё догоняется в фоне.
        # waiting_sec — сколько ИДЁТ проверка, а не сколько открыта вкладка:
        # по нему страница решает, показывать ли «занимает дольше обычного».
        from datetime import timezone
        waiting = 0
        started = rec.get("finalize_started_at") or ""
        if started:
            try:
                t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
                waiting = max(0, int((datetime.now(timezone.utc) - t0).total_seconds()))
            except Exception:  # noqa: BLE001 — битая метка не должна ломать страницу
                waiting = 0
        return JSONResponse({"status": "finalizing", "paid": True, "report_id": report_id,
                             "waiting_sec": waiting,
                             "headline": "Формируем полный отчёт — это займёт до нескольких минут…"})
    return JSONResponse({
        "status": "done", "paid": True, "report_id": report_id,
        "risk": rec.get("risk"), "headline": rec.get("headline"), "body": rec.get("body"),
        "recommendations": rec.get("recommendations", []), "checks": rec.get("checks", []),
        "report_url": f"/api/report/{report_id}",
        # Карточка машины (марка, модель, двигатель, кузов) — шапка отчёта. Без
        # неё страница начиналась с вердикта, и человек не видел подтверждения,
        # что проверяли ИМЕННО его автомобиль.
        "car": rec.get("object_preview") or {},
        "vin": rec.get("object_ref") or "",
        "created_at": rec.get("created_at") or "",
        # Паспорт ГИБДД и периоды владения — отдельные секции отчёта.
        "passport": rec.get("passport") or {},
        "owners": rec.get("owners") or [],
        # Сводка первым экраном и проверка контрольного символа VIN. Обе
        # считаются из уже полученных данных, платных запросов не стоят.
        "facts": rec.get("facts") or [],
        "vin_check": rec.get("vin_check") or {},
        "addon_pending": bool(rec.get("addon_pending")),
        "addon_applied": bool(rec.get("addon_applied")),
        "addon_result": rec.get("addon_result"),
        # Премиум-пакет (вариант D): ИНН-проверка включена без доплаты.
        "premium": orders.paid_base_variant(report_id) == "D",
        "bump_pending": bool(rec.get("bump_pending")),
        "bump_applied": bool(rec.get("bump_applied")),
        # object-only отчёт (без продавца) → на странице показываем апселл «добавить продавца».
        "object_only": bool(rec.get("object_only")) and not rec.get("seller_added"),
        "seller_addon_pending": bool(rec.get("seller_addon_pending")),
        "seller_added": bool(rec.get("seller_added")),
        "seller_addon_price": _seller_addon_delta(rec.get("variant") or "B"),
        # email покупателя в API НЕ отдаём (минимизация ПДн: report_id — capability-
        # ссылка, могла бы утечь). Форму мониторинга пользователь заполняет сам.
        "monitoring": _monitoring_state(report_id),
        # Правовые последствия находок: что произойдёт при сделке и на основании
        # чего. Считает modules/legal по правилу «код обременения → норма», иначе
        # для ипотеки показался бы текст про запрет регистрации.
        "consequences": _consequences_payload(rec),
        # Пакет к сделке — только если он оплачен. Собирается на лету из фактов
        # отчёта: хранить нечего, правила детерминированные.
        "deal_kit": _deal_kit_payload(report_id, rec),
    })


def _deal_kit_payload(report_id: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    """Пакет к сделке для страницы отчёта. None — если не куплен."""
    try:
        if not orders.paid_order_with_kit(report_id):
            return None
        from modules import dealkit
        kit = dealkit.build(rec)
        return None if kit.is_empty() else kit.to_dict()
    except Exception as e:  # noqa: BLE001 — апселл не имеет права ломать выдачу отчёта
        print(f"[dealkit] {type(e).__name__}: {e}", flush=True)
        return None


def _consequences_payload(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Последствия для страницы отчёта. Пусто — если выверенной нормы нет:
    молчание честнее выдуманного правового вывода."""
    try:
        from modules import legal
        checks = list(rec.get("checks") or [])
        # доп-проверка по ИНН лежит отдельным блоком, но последствия у неё общие
        checks += list(((rec.get("addon_result") or {}).get("checks")) or [])
        return [{"text": c.text, "severity": c.severity,
                 "norms": [{"label": n.label, "url": n.url, "note": n.note} for n in c.norms]}
                for c in legal.consequences(checks)]
    except Exception as e:  # noqa: BLE001 — отчёт важнее блока последствий
        print(f"[legal] не собрал последствия: {type(e).__name__}: {e}", flush=True)
        return []


def _monitoring_state(report_id: str) -> dict[str, Any] | None:
    """Статус мониторинга для страницы отчёта (+deep-link Telegram)."""
    try:
        from modules import monitoring
        sub = monitoring.sub_for_report(report_id)
        if not sub:
            return None
        bot = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
        return {
            "status": sub.get("status"),
            "tg_linked": bool(sub.get("tg_chat_id")),
            "tg_link": f"https://t.me/{bot}?start={sub['id']}" if bot else "",
            "expires_at": sub.get("expires_at", ""),
        }
    except Exception:
        return None


# ---------- Апселл: доп-проверка по ИНН (банкротство + арбитраж) ----------

def _bankruptcy_not_checked(report_id: str) -> bool:
    rec = _load_report(report_id)
    if not rec:
        return False
    bk = next((c for c in rec.get("checks", []) if c.get("key") == "bankruptcy"), None)
    return bool(bk and bk.get("status") == "not_checked")


def _success_html(order: dict[str, Any], report_id: str, pdf_link: str) -> str:
    """Тело страницы успеха: для addon-заказа — «проверяем», для базового — апселл."""
    if order.get("addon_seller"):
        rec = _load_report(report_id)
        if rec and not rec.get("seller_added"):
            rec["seller_addon_pending"] = True
            _save_report_record(rec)
            import threading
            threading.Thread(target=_apply_seller_addon, args=(report_id,), daemon=True).start()
        return (
            "<p>Спасибо, оплата получена. Проверяем продавца по ФССП, банкротству, судам "
            "и залогам — обычно меньше минуты. Обновите страницу — отчёт дополнится "
            "проверкой продавца.</p>"
            + (f'<a class="btn" href="/report/{report_id}/view" target="_blank" rel="noopener">'
               f'Открыть отчёт</a>' if report_id else "")
        )
    if _is_addon_order(order):
        rec = _load_report(report_id)
        if rec and not rec.get("addon_applied"):
            rec["addon_pending"] = True
            _save_report_record(rec)
            import threading
            threading.Thread(target=_apply_addon,
                             args=(report_id, str(order.get("addon_inn") or "")), daemon=True).start()
        return (
            "<p>Спасибо, оплата получена. Проверяем банкротство и арбитражные дела "
            "продавца по ИНН — это занимает 1–2 минуты. Обновите страницу через пару "
            "минут и скачайте дополненный отчёт.</p>" + pdf_link
        )
    return (
        # Перечисляем ровно то, что делает базовый тариф. Раньше здесь стояло
        # «по всем базам (банкротство, суды, приставы, залоги)», хотя банкротство
        # и залоги — отдельные доп-проверки, а суды не проверяются ни в одном тарифе.
        "<p>Оплата получена. Формируем отчёт: объект в ЕГРН (обременения, аресты, "
        "доли) и долги продавца у приставов — это займёт до нескольких минут. "
        "Отчёт откроется по кнопке ниже "
        "(страница обновится сама) и придёт на вашу почту.</p>"
        + (f'<a class="btn" href="/report/{report_id}/view" target="_blank" rel="noopener">'
           f'Открыть отчёт</a>' if report_id else "")
    )


def _apply_addon(report_id: str, inn: str) -> None:
    """Проверить банкротство+арбитраж по ИНН и дополнить готовый отчёт."""
    from modules import premium
    rec = _load_report(report_id)
    if not rec:
        return
    seller_name, seller_dob = rec.get("seller_name", ""), rec.get("seller_dob", "")
    inn = re.sub(r"\D", "", inn or "")
    # Последовательно (фон, не критично по времени): банкротство, затем
    # арбитраж отдельно — так у медленного арбитража свободный токен NewDB.
    # Адрес объекта нужен, чтобы развязать однофамильцев при поиске по ФИО:
    # у Федресурса регион приходит названием, а сопоставить его можно только
    # с адресом. Берём разобранный адрес из превью, иначе сырой ввод клиента.
    place = str((rec.get("object_preview") or {}).get("address")
                or rec.get("object_ref") or "")
    try:
        bank = check_bankruptcy(seller_name, seller_dob, inn, place_hint=place)
        arb = premium.arbitration(inn, 150.0) if inn else None
    except Exception:
        rec["addon_pending"] = False
        _save_report_record(rec)
        return
    # Доп-проверка по ИНН — ОТДЕЛЬНЫЙ блок (не мешаем в основной список).
    addon_checks = [
        Check(key="bankruptcy", name="Банкротство продавца",
              source="ЕФРСБ / Федресурс", status=bank.status, detail=bank.detail, items=bank.items),
    ]
    # Арбитраж бывает только при известном ИНН — по ФИО он не ищется. Без ИНН
    # блок не добавляем вовсе: строка «не проверено» без объяснения выглядит
    # как недоработка, хотя мы её и не обещали.
    if arb is not None:
        addon_checks.append(
            Check(key="arbitration", name="Арбитражные дела",
                  source="КАД Арбитр", status=arb.status, detail=arb.detail, items=arb.items))
    from modules.verdict import inn_analysis, compute_risk, _rule_headline
    analysis = None
    try:
        analysis = inn_analysis(seller_name, addon_checks)
    except Exception:
        analysis = None
    rec["addon_result"] = {
        "inn_mask": (inn[:4] + "…") if inn else "",
        "checks": [_serialize_check(c) for c in addon_checks],
        "analysis": analysis,
    }
    # Основные проверки не трогаем; но гейдж (уровень риска) должен учесть
    # найденное по ИНН — иначе «низкий» при найденном банкротстве вводит в заблуждение.
    base_checks = [Check(key=c["key"], name=c["name"], source=c["source"],
                         status=c["status"], detail=c["detail"], items=c.get("items", []))
                   for c in rec.get("checks", [])]
    rec["risk"] = compute_risk(base_checks + addon_checks)
    rec["headline"] = _rule_headline(rec["risk"])
    rec["addon_pending"], rec["addon_applied"] = False, True
    # PDF: базовые проверки в основной таблице + ИНН отдельной секцией (как на
    # странице). Вердикт-баннер берёт обновлённый риск, тело — базовое.
    from modules.verdict import Verdict as _Verdict
    pdf_verdict = _Verdict(risk=rec["risk"], headline=rec["headline"],
                           body=rec.get("body", ""),
                           recommendations=rec.get("recommendations", []), llm_used=True)
    pdf_path = REPORTS_DIR / f"{report_id}.pdf"
    render_report(str(pdf_path), report_id=report_id, seller_name=seller_name,
                  seller_dob=seller_dob, object_ref=rec.get("object_ref", ""),
                  email=rec.get("email", "") or "", checks=base_checks,
                  verdict=pdf_verdict, addon=rec["addon_result"],
                  deal_kit=_deal_kit_payload(report_id, rec))
    _save_report_record(rec)


def _preview_is_house_not_flat(rec: dict[str, Any]) -> bool:
    """В превью лежит ДОМ, а человеку нужна квартира.

    Флаг house_only ставится с 04.08.2026, но записи старше него его не имеют, а
    прежний код подставлял кадастр дома вместо квартиры молча — и такие отчёты мы
    перезапускаем из /admin/stuck. Поэтому проверяем ещё и по существу: тип объекта
    «здание/сооружение» при том, что во вводе назван номер квартиры.
    """
    if rec.get("house_only"):
        return True
    kind = str((rec.get("object_preview") or {}).get("type") or "").lower()
    if not ("здани" in kind or "сооружен" in kind):
        return False
    try:
        from modules import nspd
        return bool(nspd.flat_in_address(str(rec.get("object_ref") or "")))
    except Exception:  # noqa: BLE001
        return False


def _resolved_cadastre(rec: dict[str, Any]) -> str:
    """Кадастровый номер, который мы УЖЕ нашли по этому отчёту.

    Лежит в трёх местах в зависимости от пути: превью объекта, ввод пользователя
    (если он сразу дал кадастр) или текст вывода ЕГРН. Возвращаем первый
    найденный — от него зависит регион для ФССП."""
    cad = str((rec.get("object_preview") or {}).get("cad") or "").strip()
    if cad:
        return cad
    ref = str(rec.get("object_ref") or "").strip()
    if re.match(r"^\d{2}:\d{2}:\d+:\d+$", ref):
        return ref
    for c in rec.get("checks") or []:
        if c.get("key") == "object":
            m = re.search(r"(\d{2}:\d{2}:\d+:\d+)", str(c.get("detail") or ""))
            if m:
                return m.group(1)
    return ""


def _apply_seller_addon(report_id: str) -> None:
    """Апселл продавца к object-only отчёту: прогнать проверки по ФИО (ФССП/банкротство/
    арбитраж/залоги), домержить в готовый отчёт по объекту, пересчитать вердикт, пересобрать PDF."""
    rec = _load_report(report_id)
    if not rec:
        return
    seller_name = rec.get("seller_name", "")
    seller_dob = rec.get("seller_dob", "")
    if not seller_name:
        rec["seller_addon_pending"] = False
        _save_report_record(rec)
        return
    try:
        # Проверки продавца — полным тарифом (ФССП + банкротство + арбитраж + залоги).
        # #12: объект уже проверен в object-отчёте → skip_object (не платим за ЕГРН повторно).
        # Регион для ФССП берётся из кадастра, а object_ref у большинства заявок —
        # это адрес, который ввёл человек. Без skip_object регион приходил из
        # результата ЕГРН; здесь ЕГРН не дёргаем, поэтому подставляем УЖЕ
        # найденный кадастр — иначе оплаченный апселл возвращал «не удалось
        # определить регион» при том, что кадастр давно известен.
        req = CheckRequest(seller_name=seller_name, seller_dob=seller_dob,
                           object_ref=_resolved_cadastre(rec) or rec.get("object_ref") or "—",
                           tariff="ext", consent=True)
        seller_checks = [c for c in _run_checks(req, preview=False, skip_object=True)
                         if c.key in ("enforcement", "bankruptcy", "arbitration", "pledges")]
    except Exception as e:  # noqa: BLE001
        rec["seller_addon_pending"] = False
        rec["seller_addon_failed"] = datetime.utcnow().isoformat() + "Z"
        _save_report_record(rec)
        print(f"[ALERT] seller-addon failed report={report_id}: {e}", flush=True)
        return
    # Если ни одна проверка продавца не прошла (баланс/источник) — не помечаем добавленным,
    # оставляем на ретрай, деньги за неполный апселл не «сгорают» молча.
    if seller_checks and all(c.status == "not_checked" for c in seller_checks):
        rec["seller_addon_pending"] = False
        rec["seller_addon_failed"] = datetime.utcnow().isoformat() + "Z"
        _save_report_record(rec)
        _bump_metric("finalize_failed")
        print(f"[ALERT] seller-addon: все проверки продавца not_checked report={report_id}", flush=True)
        return
    # Мерж: объектные проверки (уже в rec) + новые проверки продавца.
    obj_checks = [Check(key=c["key"], name=c["name"], source=c["source"],
                        status=c["status"], detail=c["detail"], items=c.get("items", []))
                  for c in rec.get("checks", [])]
    # Повтор (ретрай после сбоя, ручной перезапуск) не должен множить строки:
    # старые результаты по тем же реестрам заменяем, а не дописываем — иначе
    # в отчёте два «Исполнительных производства» с разными выводами.
    fresh = {c.key for c in seller_checks}
    obj_checks = [c for c in obj_checks if c.key not in fresh]
    all_checks = obj_checks + seller_checks
    verdict = build_verdict(all_checks, seller_name=seller_name)
    rec.update({"risk": verdict.risk, "headline": verdict.headline, "body": verdict.body,
                "recommendations": verdict.recommendations, "llm_used": verdict.llm_used,
                "checks": [_serialize_check(c) for c in all_checks]})
    rec["seller_added"], rec["seller_addon_pending"] = True, False
    rec.pop("seller_addon_failed", None)
    try:
        render_report(str(REPORTS_DIR / f"{report_id}.pdf"), report_id=report_id,
                      seller_name=seller_name, seller_dob=seller_dob,
                      object_ref=rec.get("object_ref", ""), email=rec.get("email", "") or "",
                      checks=all_checks, verdict=verdict,
                      deal_kit=_deal_kit_payload(report_id, rec))
    except Exception:  # noqa: BLE001
        pass
    _save_report_record(rec)


_SAVE_LOCK = threading.Lock()


def _save_report_record(rec: dict[str, Any]) -> None:
    """Сохранить запись отчёта АТОМАРНО.

    Раньше писали прямо в целевой файл. Два потока (финализация и апселл) открывали
    его одновременно, каждый усекал при открытии и писал с нулевого смещения — и
    хвост более длинной версии оставался за концом более короткой. Получался JSON
    с мусором после закрывающей скобки, файл переставал читаться, а отчёт по
    оплаченному заказу становился недоступен (78eab183392d, 798 ₽, 05.08.2026).

    Пишем во временный файл и переименовываем: os.replace атомарен в пределах ФС,
    читатель видит либо старую версию целиком, либо новую целиком.
    """
    REPORTS[rec["id"]] = rec
    path = REPORTS_DIR / f"{rec['id']}.json"
    tmp = path.with_suffix(f".json.tmp{os.getpid()}")
    try:
        with _SAVE_LOCK:
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


class AddonInterestRequest(BaseModel):
    report_id: str = ""


@app.post("/api/addon-interest")
def api_addon_interest(req: AddonInterestRequest) -> JSONResponse:
    """Клик по тизеру апселла (открыл модалку с ИНН). Считаем воронку
    интерес→оплата, чтобы видеть, режет ли цена. Fire-and-forget."""
    try:
        abstats.bump_counter(orders.PAYMENTS_LOG.parent, "addon_interest")
    except Exception:
        pass
    return JSONResponse({"ok": True})


class AddonInnRequest(BaseModel):
    report_id: str
    inn: str


class AddonSellerRequest(BaseModel):
    """Апселл продавца к оплаченному object-only отчёту: доплата дельты → проверка
    ФИО по ФССП/банкротству/арбитражу → мерж в готовый отчёт."""
    report_id: str
    seller_name: str = Field("", description="ФИО продавца")
    seller_dob: str = Field("", description="Дата рождения ДД.ММ.ГГГГ")
    consent: bool = Field(False, description="Согласие на обработку ПДн продавца (152-ФЗ)")
    consent_text: str = Field("", max_length=600, description="Текст согласия, показанный человеку")
    consent_version: str = Field("", max_length=20, description="Редакция формулировки")


class RecheckRequest(BaseModel):
    """Повторная проверка залога по оплаченному отчёту, перед передачей денег."""
    report_id: str


@app.post("/api/recheck-pledge")
def api_recheck_pledge(req: RecheckRequest, request: Request) -> JSONResponse:
    rec = _load_report(req.report_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Отчёт не найден.")
    if not orders.is_report_paid(req.report_id):
        raise HTTPException(status_code=402, detail="Сначала оплатите проверку автомобиля.")
    amount = TARIFFS["recheck"]["amount"]
    order = orders.new_order(tariff="recheck", amount=amount, report_id=req.report_id)
    orders.update_order(order["id"], src=_get_src(request),
                        campaign=request.cookies.get("cs_camp", "") or "прямой/органика",
                        **_order_context(request, req.report_id))
    return_url = f"{_app_base(request)}/pay/return?order={order['id']}"
    try:
        intent = create_payment("recheck", order["id"], return_url, email=rec.get("email", ""))
    except YooKassaError as exc:
        orders.update_order(order["id"], status="failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"ЮKassa: {exc}") from exc
    orders.update_order(order["id"], payment_id=intent.id, status=intent.status, mode=intent.mode)
    return JSONResponse({"confirmation_url": intent.confirmation_url,
                         "mode": intent.mode, "amount": amount})


def _apply_recheck(report_id: str) -> None:
    """Выполнить оплаченную повторную проверку залога и вписать её в отчёт."""
    rec = _load_report(report_id)
    if not rec:
        return
    try:
        from modules import auto as _auto
        fresh = _auto.recheck_pledge(str(rec.get("object_ref") or ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[recheck] {report_id}: {type(exc).__name__}: {exc}", flush=True)
        # Флаг ретрая: оплату мы взяли, значит услугу обязаны довезти.
        rec["recheck_failed"] = True
        _save_report_record(rec)
        return
    checks = rec.get("checks") or []
    row = {"key": "pledge", "name": fresh.name, "source": fresh.source,
           "status": fresh.status, "detail": fresh.detail, "items": fresh.items}
    for i, c in enumerate(checks):
        if c.get("key") == "pledge":
            checks[i] = row
            break
    else:
        checks.append(row)
    rec["checks"] = checks
    rec["recheck_at"] = datetime.utcnow().isoformat() + "Z"
    rec.pop("recheck_failed", None)
    # Залог нашёлся при повторной проверке — риск отчёта обязан стать высоким,
    # иначе в документе останется зелёный вердикт над красной строкой.
    if fresh.status == "found":
        from modules.verdict import RISK_HIGH
        rec["risk"] = RISK_HIGH
        rec["headline"] = "Покупать нельзя: машина в залоге"
    _save_report_record(rec)
    print(f"[recheck] {report_id}: залог={fresh.status}", flush=True)


class UpgradeRequest(BaseModel):
    """Доплата с базового тарифа до полного по уже оплаченному отчёту."""
    report_id: str


@app.post("/api/upgrade")
def api_upgrade(req: UpgradeRequest, request: Request) -> JSONResponse:
    """Открыть блоки, не вошедшие в базовый тариф, за разницу в цене.

    Отдельный тариф, а не повторная покупка: иначе человек платит 199 + 449 за
    один и тот же автомобиль и совершенно справедливо требует возврат.
    """
    rec = _load_report(req.report_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Отчёт не найден.")
    if not orders.is_report_paid(req.report_id):
        raise HTTPException(status_code=402, detail="Сначала оплатите базовую проверку.")
    if not rec.get("object_only"):
        raise HTTPException(status_code=400, detail="Отчёт уже полный — доплачивать нечего.")
    # Повтор из второй вкладки после оплаты: второй платёж не создаём.
    if orders.paid_order_exists(req.report_id, "upgrade"):
        return JSONResponse({"already": True,
                             "message": "Доплата уже прошла — отчёт дополняется."})
    amount = PRICE_FULL - PRICE_OBJECT
    order = orders.new_order(tariff="upgrade", amount=amount, report_id=req.report_id)
    orders.update_order(order["id"], src=_get_src(request),
                        campaign=request.cookies.get("cs_camp", "") or "прямой/органика",
                        **_order_context(request, req.report_id))
    return_url = f"{_app_base(request)}/pay/return?order={order['id']}"
    try:
        intent = create_payment("upgrade", order["id"], return_url,
                                email=rec.get("email", ""), amount_override=amount)
    except YooKassaError as exc:
        orders.update_order(order["id"], status="failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"ЮKassa: {exc}") from exc
    orders.update_order(order["id"], payment_id=intent.id, status=intent.status, mode=intent.mode)
    # Отчёт станет полным только после оплаты: снимаем object_only в финализаторе,
    # а не здесь, иначе неоплаченная доплата уже открыла бы блоки.
    return JSONResponse({"confirmation_url": intent.confirmation_url,
                         "mode": intent.mode, "amount": amount})


@app.post("/api/addon-inn")
def api_addon_inn(req: AddonInnRequest, request: Request) -> JSONResponse:
    rec = _load_report(req.report_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Отчёт не найден.")
    if not orders.is_report_paid(req.report_id):
        raise HTTPException(status_code=402, detail="Сначала оплатите базовый отчёт.")
    # #14 (152-ФЗ): ИНН — ПДн продавца; требуем согласие, зафиксированное при покупке отчёта.
    if not rec.get("consent"):
        raise HTTPException(status_code=400,
                            detail="Требуется согласие на обработку персональных данных (152-ФЗ).")
    # #6: уже выполняется/выполнена — не создаём второй платёж (повтор из старой вкладки → 2×249₽).
    if rec.get("addon_applied") or rec.get("addon_pending"):
        return JSONResponse({"already": True, "message": "Проверка по ИНН уже выполняется/выполнена."})
    # ИНН НЕОБЯЗАТЕЛЕН. Раньше без него был отказ 400 — и апселл не покупал никто:
    # у покупателя квартиры ИНН продавца нет и быть не должно. Поиск по Федресурсу
    # идёт по ФИО, ИНН приходит В ОТВЕТЕ. Если клиент ИНН всё-таки знает — берём,
    # он сразу снимает вопрос однофамильцев.
    inn = re.sub(r"\D", "", req.inn or "")
    if inn and len(inn) not in (10, 12):
        raise HTTPException(status_code=400, detail="ИНН — 10 или 12 цифр (или оставьте поле пустым).")
    if not inn and not (rec.get("seller_name") or "").strip():
        raise HTTPException(status_code=400, detail=(
            "Для проверки банкротства нужны ФИО продавца — добавьте их к отчёту "
            "или укажите ИНН."))
    # ПРЕМИУМ (вариант D): ИНН-проверка включена в пакет — запускаем без оплаты.
    # ГАРД: один раз на отчёт (иначе повторные клики → параллельные _apply_addon
    # + бесплатная гонка проверок по произвольным ИНН за наш счёт у NewDB).
    if orders.paid_base_variant(req.report_id) == "D":
        # Через тот же слот, что и платные апселлы: клиент не доплачивает, но NewDB
        # мы платим — значит и потолок попыток должен действовать.
        if not _claim_addon_slot(req.report_id, "addon"):
            return JSONResponse({"included": True,
                                 "message": "Проверка по ИНН уже выполняется/выполнена."})
        import threading
        threading.Thread(target=_apply_addon, args=(req.report_id, inn), daemon=True).start()
        return JSONResponse({"included": True,
                             "message": "Проверка по ИНН включена в ваш Премиум-пакет — запустили."})
    # #3: уже оплачен ИНН-апселл (повтор из второй вкладки после оплаты) → не берём второй раз.
    if orders.paid_order_exists(req.report_id, "addon"):
        _maybe_trigger_addon({"addon_inn": inn}, req.report_id)
        return JSONResponse({"already": True, "message": "Оплата по ИНН уже прошла — выполняем проверку."})
    amount = TARIFFS["addon"]["amount"]
    order = orders.new_order(tariff="addon", amount=amount, report_id=req.report_id)
    # #17: атрибуция апселла к тому же источнику/кампании, что первичный отчёт (иначе выручка в органику).
    orders.update_order(order["id"], addon_inn=inn, src=_get_src(request),
                        campaign=request.cookies.get("cs_camp", "") or "прямой/органика",
                        **_order_context(request, req.report_id))
    return_url = f"{_app_base(request)}/pay/return?order={order['id']}"
    try:
        intent = create_payment("addon", order["id"], return_url, email=rec.get("email", ""))
    except YooKassaError as exc:
        orders.update_order(order["id"], status="failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"ЮKassa: {exc}") from exc
    orders.update_order(order["id"], payment_id=intent.id, status=intent.status, mode=intent.mode)
    if intent.mode == "bypass":
        rec["addon_pending"] = True
        _save_report_record(rec)
        import threading
        threading.Thread(target=_apply_addon, args=(req.report_id, inn), daemon=True).start()
    return JSONResponse({"confirmation_url": intent.confirmation_url, "mode": intent.mode,
                         "amount": amount})


@app.post("/api/addon-seller")
def api_addon_seller(req: AddonSellerRequest, request: Request) -> JSONResponse:
    """Апселл продавца к оплаченному object-only отчёту. Доплата дельты (полная цена варианта
    − цена объекта того же варианта, из СОХРАНЁННОГО заказа; см. _seller_addon_delta),
    затем проверка ФИО и мерж в отчёт. Итоговая сумма = как если бы сразу купили полный отчёт."""
    rec = _load_report(req.report_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Отчёт не найден.")
    if not orders.is_report_paid(req.report_id):
        raise HTTPException(status_code=402, detail="Сначала оплатите проверку объекта.")
    if not rec.get("object_only"):
        raise HTTPException(status_code=400, detail="Продавец добавляется только к отчёту по объекту.")
    if rec.get("preview"):  # L8: объектный отчёт ещё формируется/сбоил — нельзя мержить плейсхолдеры
        raise HTTPException(status_code=409,
                            detail="Отчёт по объекту ещё формируется — обновите страницу через минуту.")
    if rec.get("seller_added") or rec.get("seller_addon_pending"):
        return JSONResponse({"already": True, "message": "Проверка продавца уже выполняется/выполнена."})
    # #3: уже есть оплаченный заказ апселла продавца (повтор из второй вкладки после оплаты,
    # флаг выполнения ещё не проставлен асинхронно) → не создаём второй платёж.
    if orders.paid_order_exists(req.report_id, "addon_seller"):
        _maybe_trigger_seller_addon({"addon_seller": 1}, req.report_id)  # добьём выполнение
        return JSONResponse({"already": True, "message": "Оплата продавца уже прошла — выполняем проверку."})
    # Уже оплачено, но прошлая проверка сбоила (баланс/источник) — ПОВТОРЯЕМ без новой оплаты,
    # чтобы не списать второй раз. Флаг ставит только сервер после платного прогона.
    # Ветка платная, поэтому проходит те же два ограничителя, что и всё остальное:
    # гейт провайдера (раньше стоял НИЖЕ и не действовал на этот путь) и счётчик
    # попыток (раньше его не было вовсе — клиент мог жать «повторить» бесконечно).
    if rec.get("seller_addon_failed") and rec.get("seller_name"):
        _provider_degraded()          # только метрика и лог: повтор всё равно бесплатный
        if not _claim_addon_slot(req.report_id, "seller"):
            return JSONResponse({"already": True, "message": (
                "Проверка продавца уже выполняется либо попытки исчерпаны — "
                "мы разберёмся вручную и свяжемся с вами.")})
        rec = _load_report(req.report_id) or rec
        rec.pop("seller_addon_failed", None)
        _save_report_record(rec)
        import threading
        threading.Thread(target=_apply_seller_addon, args=(req.report_id,), daemon=True).start()
        return JSONResponse({"retry": True, "message": "Повторяем проверку продавца — оплата уже прошла."})
    if not req.consent:
        raise HTTPException(status_code=400,
                            detail="Требуется согласие на обработку персональных данных продавца (152-ФЗ).")
    fio = " ".join((req.seller_name or "").split())
    if len(fio.split()) < 2:
        raise HTTPException(status_code=400, detail="Укажите ФИО продавца полностью (фамилия и имя).")
    # Дата рождения — не «уточнение», а условие выполнимости: без неё ФССП отказывает,
    # банкротство упирается в однофамильцев (в живом случае — 30 человек), арбитраж
    # требует ИНН. Раньше поле было необязательным на обоих концах, и заказ 28.07
    # (Тамбов, 250 ₽) закономерно вернул три «не проверено». Брать деньги за заведомо
    # невыполнимую проверку нельзя — отказываем ДО создания платежа.
    if not _DOB_RE.match((req.seller_dob or "").strip()):
        raise HTTPException(status_code=400, detail=(
            "Нужна дата рождения продавца в формате ДД.ММ.ГГГГ — без неё в реестрах "
            "не отличить нужного человека от однофамильцев."))
    _provider_degraded()              # метрика; деньги берём, отчёт довезём ретраем
    # H2: дельта по варианту, ВЗЯТОМУ ИЗ СОХРАНЁННОГО заказа объекта (не из живой куки —
    # иначе подмена cs_v2 занизила бы). object + delta = полная цена варианта.
    delta = _seller_addon_delta(rec.get("variant") or "B")
    # Сохраняем ФИО в отчёт — применение после оплаты по нему проверит продавца.
    rec["seller_name"] = fio[:120]
    if req.seller_dob:
        rec["seller_dob"] = req.seller_dob.strip()[:10]
    _record_consent(rec, request, req.consent_text, req.consent_version)
    _save_report_record(rec)
    order = orders.new_order(tariff="addon_seller", amount=delta, report_id=req.report_id)
    # #17: атрибуция апселла к источнику/кампании (иначе выручка апселла уезжает в органику).
    orders.update_order(order["id"], addon_seller=1, src=_get_src(request),
                        campaign=request.cookies.get("cs_camp", "") or "прямой/органика",
                        **_order_context(request, req.report_id))
    return_url = f"{_app_base(request)}/pay/return?order={order['id']}"
    try:
        intent = create_payment("addon_seller", order["id"], return_url,
                                email=rec.get("email", ""), amount_override=delta)
    except YooKassaError as exc:
        orders.update_order(order["id"], status="failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"ЮKassa: {exc}") from exc
    orders.update_order(order["id"], payment_id=intent.id, status=intent.status, mode=intent.mode)
    if intent.mode == "bypass":
        rec["seller_addon_pending"] = True
        _save_report_record(rec)
        import threading
        threading.Thread(target=_apply_seller_addon, args=(req.report_id,), daemon=True).start()
    return JSONResponse({"confirmation_url": intent.confirmation_url, "mode": intent.mode, "amount": delta})


# ---------- Мониторинг продавца до сделки (подписка) ----------

class MonitorRequest(BaseModel):
    report_id: str
    email: str
    days: int = 30
    consent: bool = False


@app.post("/api/monitor")
def api_monitor(req: MonitorRequest, request: Request) -> JSONResponse:
    if not req.consent:
        raise HTTPException(status_code=400, detail="Требуется согласие на обработку ПДн (152-ФЗ).")
    rec = _load_report(req.report_id)  # диск-фолбэк
    if not rec:
        raise HTTPException(status_code=404, detail="Отчёт не найден — сначала выполните проверку.")
    from modules import monitoring
    # Подписка создаётся в статусе ожидания оплаты; активируется по webhook.
    sub = monitoring.create_subscription(
        email=req.email, seller_name=rec["seller_name"], seller_dob=rec.get("seller_dob", ""),
        object_ref=rec.get("object_ref", ""), seller_inn=rec.get("seller_inn", ""),
        days=max(7, min(req.days, 180)), baseline_checks=rec.get("checks", []),
        status="pending_payment", report_id=req.report_id,
    )
    # Платёж за мониторинг (тариф monitor). Активация — в webhook по order.monitor_sub_id.
    amount = TARIFFS["monitor"]["amount"]
    order = orders.new_order(tariff="monitor", amount=amount, report_id=req.report_id)
    orders.update_order(order["id"], monitor_sub_id=sub["id"],
                        **_order_context(request, req.report_id))
    return_url = f"{_app_base(request)}/pay/return?order={order['id']}"
    try:
        intent = create_payment("monitor", order["id"], return_url, email=req.email)
    except YooKassaError as exc:
        orders.update_order(order["id"], status="failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"ЮKassa: {exc}") from exc
    orders.update_order(order["id"], payment_id=intent.id, status=intent.status, mode=intent.mode)
    # bypass-режим (нет боевой ЮKassa) — активируем сразу, чтобы демо работало.
    if intent.mode == "bypass":
        monitoring.activate(sub["id"])
    bot = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
    tg_link = f"https://t.me/{bot}?start={sub['id']}" if bot else ""
    return JSONResponse({
        "subscription_id": sub["id"], "amount": amount,
        "confirmation_url": intent.confirmation_url, "mode": intent.mode,
        "telegram_link": tg_link,
    })


@app.post("/api/tg/webhook")
async def api_tg_webhook(request: Request) -> JSONResponse:
    """Telegram присылает апдейты сюда. Обрабатываем /start <sub_id> —
    привязываем chat_id пользователя к подписке для отправки алертов."""
    from modules import monitoring
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": True})
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if text.startswith("/start") and chat_id:
        parts = text.split(maxsplit=1)
        sub_id = parts[1].strip() if len(parts) > 1 else ""
        if sub_id and monitoring.link_telegram(sub_id, chat_id):
            monitoring.send_telegram(
                chat_id,
                "✓ Telegram подключён к мониторингу продавца.\n"
                "Пришлём сюда сообщение, если в госреестрах появится красный флаг "
                "(банкротство, долги, аресты, суды).",
            )
        else:
            monitoring.send_telegram(
                chat_id,
                "Здравствуйте! Это бот уведомлений сервиса ЧистаяСделка. "
                "Чтобы подключить мониторинг, оформите его на https://chistasdelka.ru",
            )
    return JSONResponse({"ok": True})


@app.get("/api/monitor/list")
def api_monitor_list(email: str) -> JSONResponse:
    from modules import monitoring
    subs = monitoring.subs_for_email(email)
    return JSONResponse({"subscriptions": [
        {"id": s["id"], "seller_name": s["seller_name"], "object_ref": s.get("object_ref", ""),
         "status": s["status"], "expires_at": s["expires_at"], "runs": s.get("runs", 0),
         "alerts": len(s.get("alerts", []))}
        for s in subs
    ]})


@app.post("/api/monitor/cancel/{sub_id}")
def api_monitor_cancel(sub_id: str) -> JSONResponse:
    from modules import monitoring
    ok = monitoring.cancel_subscription(sub_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    return JSONResponse({"status": "cancelled"})


@app.on_event("startup")
def _start_monitor_scheduler() -> None:
    from modules import monitoring
    monitoring.start_scheduler()
    try:
        from modules import adminbot
        adminbot.start_sweeper()   # сторож: оплачено, но услуга не оказана
    except Exception as e:  # noqa: BLE001
        print(f"[admin] сторож не запустился: {type(e).__name__}: {e}", flush=True)
    # ОПРОС БОТА ЗДЕСЬ НЕ ЗАПУСКАЕМ. Токен у нас общий с chistasdelka.ru, а
    # Telegram допускает только один getUpdates на токен: второй опросчик рвёт
    # первому соединение («Conflict: terminated by other getUpdates request»)
    # и ломает команды в рабочем боте недвижимости.
    # Отправку уведомлений это не затрагивает — sendMessage конфликта не даёт,
    # поэтому сторож недоставленных оплат работает и отсюда.
    if os.getenv("TG_POLLING", "").strip() == "1":
        monitoring.start_polling()
    # #3: НАБЛЮДАТЕЛЬ за застрявшими финализациями. Только замечает и зовёт человека —
    # платных обращений к источникам не делает (см. _watch_stuck_finalizations).
    try:
        import threading
        threading.Thread(target=_watch_stuck_finalizations, daemon=True).start()
        # СБОРЩИК оплаченных, но не дождавшихся ответов. Единственная фоновая
        # работа, которой разрешено ходить в NewDB: опрос по requestId
        # не тарифицируется. Он ничего не отправляет — только забирает своё.
        threading.Thread(target=_sweep_pending_loop, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


# ────────────────────────────────────────────────────────────────────────────
# ПРАВИЛО: автоматика НИКОГДА не тратит деньги сама.
#
# Раньше здесь жил фоновый цикл, который сам перезапускал упавшие финализации.
# Он же 25.07.2026 сжёг баланс: один и тот же адрес («Город Калуга ул дружба
# д 10 кв 13 5 этаж») ушёл в платный Росреестр 132 раза, и 131 раз сервис
# ЧЕСТНО ответил «объекта нет в ЕГРН» — то есть повторять было бессмысленно
# с самой первой попытки. Цикл не умел отличить «сервис лёг» от «объекта нет».
#
# Теперь вместо него наблюдатель: находит оплаченные, но не выданные отчёты,
# пишет алерт — и всё. Перезапуск делает человек кнопкой в /admin/stuck.
# Это дороже по вниманию, но не может утечь ночью в ноль.
# ────────────────────────────────────────────────────────────────────────────

_WATCH_PERIOD_SEC = 1800          # раз в 30 мин: скан бесплатный, чаще незачем


# Автододелка сбойных финализаций. Ограничители: не чаще раза в 15 минут на
# отчёт и не больше трёх попыток — платные базы дёргать бесконечно нельзя.
_RETRY_AFTER_SEC = 15 * 60
# Берём из adminbot, а не объявляем своё: сторож молчит ровно до тех пор, пока
# попытки не исчерпаны, и разъехавшиеся числа вернули бы тревогу раньше починки.
from modules.adminbot import RETRY_MAX as _RETRY_MAX  # noqa: E402


def _retry_stuck_once(stuck: list[dict]) -> int:
    """Повторить финализацию у оплаченных, но не выданных отчётов.

    Раньше наблюдатель только печатал алерт, а доделывал человек руками — за
    06.08 так пришлось вытаскивать четыре заказа, причём кнопка в админке при
    этом молча не срабатывала. Клиент всё это время сидел с оплатой и без
    отчёта. Теперь повтор идёт сам.

    Тратим платные запросы, поэтому: только когда источник жив, не чаще раза в
    15 минут на отчёт и максимум три попытки. Дальше — human-разбор, как и было.
    """
    if _provider_degraded():          # источник лежит — повтор всё равно упадёт
        return 0
    started = 0
    now = datetime.utcnow()
    for s in stuck:
        rid = s.get("report_id") or ""
        rec = _load_report(rid)
        if not rec or not rec.get("preview") or s.get("hopeless"):
            continue
        if int(rec.get("finalize_attempts") or 0) >= _RETRY_MAX:
            continue
        last_try = rec.get("finalize_failed") or rec.get("finalize_started_at") or ""
        try:
            when = datetime.fromisoformat(str(last_try).replace("Z", ""))
            if (now - when).total_seconds() < _RETRY_AFTER_SEC:
                continue
        except Exception:  # noqa: BLE001 — нет отметки времени: пробуем
            pass
        rec["finalize_attempts"] = int(rec.get("finalize_attempts") or 0) + 1
        _save_report_record(rec)
        print(f"[retry] автоповтор финализации report={rid} "
              f"попытка {rec['finalize_attempts']}/{_RETRY_MAX}", flush=True)
        _bump_metric("finalize_auto_retry")
        threading.Thread(
            target=_finalize_then_notify,
            args=(rid, s.get("email") or "", (s.get("tariff") == "object")),
            daemon=True).start()
        started += 1
    return started


def _watch_stuck_finalizations() -> None:
    """Периодически искать ОПЛАЧЕННЫЕ, но не выданные отчёты: сигналить и
    повторять финализацию автоматически (см. _retry_stuck_once). Алерт
    печатается только когда набор застрявших изменился — иначе лог заплывёт."""
    import time
    time.sleep(8)  # дать скедулеру подняться
    last: set[str] = set()
    while True:
        try:
            stuck = _find_stuck_paid()
            ids = {s["report_id"] for s in stuck}
            if ids:
                _retry_stuck_once(stuck)
            if ids and ids != last:
                print(f"[stuck] оплачено без отчёта: {len(stuck)} — "
                      f"разбор в /admin/stuck", flush=True)
                for s in stuck:
                    print(f"[stuck]   {s['report_id']} | {s['amount']}руб "
                          f"[{s['tariff']}] | {s['email']} | {s['object_ref'][:50]}",
                          flush=True)
                _bump_metric("stuck_detected")
            elif not ids and last:
                print("[stuck] все оплаченные отчёты выданы", flush=True)
            last = ids
        except Exception as e:  # noqa: BLE001 — наблюдатель не имеет права падать
            print(f"[stuck] scan failed: {e}", flush=True)
        time.sleep(_WATCH_PERIOD_SEC)


# Как часто забирать доехавшие ответы. Опрос по requestId БЕСПЛАТЕН, поэтому
# здесь можно и нужно чаще, чем раз в полчаса: чем раньше забрали — тем раньше
# перезапуск отчёта станет бесплатным. По наблюдениям 25.07 их внутренний
# restart укладывается в 20-40 минут, так что 3 минуты с запасом.
_SWEEP_PERIOD_SEC = 180


def _sweep_pending_loop() -> None:
    """Забирать оплаченные, но не дождавшиеся ответа результаты NewDB.

    ЭТО НЕ НАРУШАЕТ ПРАВИЛО «автоматика не тратит сама»: sweep_pending только
    опрашивает уже созданные requestId, а опрос не тарифицируется. Ни одного
    нового запроса отсюда не уходит — мы просто забираем то, за что заплатили.

    Забранное ложится в кэш, и следующий прогон отчёта (кнопкой человека в
    /admin/stuck) не платит за эти проверки повторно."""
    import time
    time.sleep(20)  # дать приложению подняться
    while True:
        try:
            from modules import newdb
            st = newdb.sweep_pending()
            if st["ready"]:
                print(f"[pending] забрано даром: {st['ready']}, ещё ждём: {st['waiting']} — "
                      f"перезапуск этих отчётов теперь бесплатный", flush=True)
                _bump_metric("pending_recovered")
        except Exception as e:  # noqa: BLE001 — сборщик не имеет права падать
            print(f"[pending] sweep failed: {e}", flush=True)
        time.sleep(_SWEEP_PERIOD_SEC)


def _find_stuck_paid() -> list[dict[str, Any]]:
    """Оплаченные заказы, по которым отчёт так и остался превью. Чистое чтение
    с диска: ни одного платного запроса, безопасно дёргать сколько угодно."""
    out: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for path in orders.ORDERS_DIR.glob("*.json"):
        try:
            o = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if o.get("status") not in ("succeeded", "bypass"):
            continue
        rid = o.get("report_id") or ""
        tar = o.get("tariff") or ""
        if not rid or tar not in ("base", "object", "ext", "realtor"):
            continue
        prev = seen.get(rid)
        if prev:
            prev["amount"] = int(prev.get("amount") or 0) + int(o.get("amount") or 0)
            continue
        seen[rid] = {"report_id": rid, "tariff": tar,
                     "amount": int(o.get("amount") or 0),
                     "email": o.get("email") or "",
                     "paid_at": o.get("paid_at") or o.get("created_at") or ""}
    for rid, info in seen.items():
        r = _load_report(rid)
        if not (r and r.get("preview")):
            continue
        # Отложенные вручную: решение по ним принято (ждём ответ клиента, ждём
        # возврат, регион не поддерживаем) и напоминать больше не о чем. Без этого
        # сторож перечисляет их при КАЖДОМ новом застрявшем заказе, и настоящая
        # новая проблема тонет в списке уже разобранных.
        if r.get("deferred"):
            continue
        info["email"] = info["email"] or (r.get("email") or "")
        info["object_ref"] = str(r.get("object_ref") or "")
        info["attempts"] = int(r.get("finalize_attempts") or 0)
        # объекта нет в ЕГРН — перезапуск платный и заведомо бесполезный, нужен возврат
        info["hopeless"] = bool(r.get("finalize_permanent"))
        out.append(info)
    return sorted(out, key=lambda x: x.get("paid_at") or "")


SCHEMES_DIR = Path(os.getenv("GENERATED_DIR", "data/generated")).parent / "schemes"


def _save_dtp_schemes(report_id: str, checks: list) -> None:
    """Скачать схемы повреждений и подменить ссылки на свои.

    Схема — самая наглядная часть отчёта о ДТП: жёлтым закрашены зоны удара.
    Отдаём её со своего домена, а не ссылкой на CDN поставщика.
    """
    import httpx          # локально, как и в остальных местах файла
    SCHEMES_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for c in checks:
        if getattr(c, "key", "") != "dtp":
            continue
        for item in (getattr(c, "items", None) or []):
            url = str((item or {}).get("schemeUrl") or "")
            if not url.startswith("http"):
                continue
            n += 1
            dest = SCHEMES_DIR / f"{report_id}-{n}.png"
            try:
                with httpx.Client(timeout=20, follow_redirects=True) as cl:
                    r = cl.get(url)
                if r.status_code == 200 and len(r.content) > 500:
                    dest.write_bytes(r.content)
                    item["schemeUrl"] = f"/report/{report_id}/scheme/{n}.png"
                else:
                    print(f"[scheme] {url} → HTTP {r.status_code}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[scheme] {type(exc).__name__}: {exc}", flush=True)


@app.get("/report/{report_id}/scheme/{idx}.png")
def report_scheme(report_id: str, idx: str) -> Any:
    """Схема повреждений. Часть платного отчёта, поэтому под тем же гейтом."""
    if report_id != "example" and not orders.is_report_paid(report_id):
        raise HTTPException(status_code=402, detail="Отчёт доступен после оплаты.")
    safe = re.sub(r"[^0-9]", "", idx) or "1"
    path = SCHEMES_DIR / f"{report_id}-{safe}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Схема не найдена.")
    return FileResponse(str(path), media_type="image/png")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> PlainTextResponse:
    """Отчёты клиентов из индекса закрыты наглухо: report_id — это ссылка-ключ,
    по которой отчёт открывается без пароля, и попадание её в поиск означало бы
    публикацию чужой проверки. Лендинг и пример открыты — они и есть витрина."""
    return PlainTextResponse(
        "User-agent: *\n"
        "Disallow: /report/\n"
        "Disallow: /pay/\n"
        "Disallow: /admin/\n"
        "Disallow: /api/\n"
        "Allow: /example\n"
        "Allow: /\n\n"
        "Sitemap: https://avto.chistasdelka.ru/sitemap.xml\n"
    )


@app.get("/sitemap.xml", response_class=PlainTextResponse)
def sitemap() -> PlainTextResponse:
    pages = ["/", "/example", "/offer", "/privacy", "/consent"]
    urls = "".join(
        f"<url><loc>https://avto.chistasdelka.ru{p}</loc>"
        f"<changefreq>{'weekly' if p in ('/', '/example') else 'yearly'}</changefreq></url>"
        for p in pages)
    return PlainTextResponse(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + urls + "</urlset>",
        media_type="application/xml")


@app.get("/example", response_class=HTMLResponse)
def example_view() -> HTMLResponse:
    """Пример отчёта — открыт всем, без оплаты и без заявки.

    Главный аргумент в нише: человек не понимает, за что платит, пока не увидел
    готовый отчёт. Конкуренты держат такую страницу на видном месте, и это
    единственная их страница, которую можно показать в рекламе как есть.
    """
    return HTMLResponse((STATIC_DIR / "report.html").read_text(encoding="utf-8"))


@app.get("/api/example-report")
def api_example_report() -> JSONResponse:
    """Данные страницы-примера. Настоящий отчёт по реальной машине, снятый
    один раз и сохранённый файлом: VIN замаскирован, платных запросов ноль.
    Показывать выдуманные находки на витрине нельзя — это то же враньё, что и
    в платном отчёте, только на входе."""
    path = STATIC_DIR / "example_report.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Пример пока не подготовлен.")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.get("/report/{report_id}/view", response_class=HTMLResponse)
def report_view(report_id: str) -> HTMLResponse:
    """Красивая страница отчёта (спидометр + проверки + апселл). Данные тянет
    из /api/check/full (гейт по оплате)."""
    return HTMLResponse((STATIC_DIR / "report.html").read_text(encoding="utf-8"))


def _send_object_missing_email(email: str, report_id: str, rec: dict[str, Any]) -> None:
    """Оплата прошла, но объект не нашёлся ни в одной базе.

    Раньше человек в этой ситуации получал обычное «ваш отчёт готов» и открывал
    заключение без главной части. Теперь просим точный кадастровый номер и обещаем
    доделать без доплаты — плюс сигнал оператору, чтобы довести вручную.
    """
    if not email or not os.getenv("UNISENDER_GO_API_KEY"):
        return
    # Спрашиваем номер РОВНО ОДИН РАЗ за отчёт. Раньше защита стояла на
    # ready_email_sent — флаге «отчёт отдан», который законно снимается при любой
    # пересдаче. 07.08 отчёт a907a7ec859a пересобирали дважды, и клиентка трижды
    # получила «пришлите кадастровый номер», хотя дважды его прислала. ask_sent
    # не снимается никогда: спросили — значит спросили.
    if rec.get("ask_sent") or rec.get("ready_email_sent"):
        return
    from html import escape
    base = os.getenv("PUBLIC_BASE", "https://chistasdelka.ru").rstrip("/")
    url = f"{base}/report/{report_id}/view"
    ref = (rec.get("object_ref_original") or rec.get("object_ref") or "").strip()
    text = (
        "Здравствуйте!\n\nОплата получена, проверку мы запустили. По объекту, который вы "
        "указали (%s), сведения в реестрах не нашлись — такое бывает, когда адрес записан "
        "иначе, чем в ЕГРН, или когда вместо кадастрового номера указан условный.\n\n"
        "Пришлите, пожалуйста, в ответ на это письмо кадастровый номер объекта — вида "
        "77:01:0004012:1122. Он есть в выписке ЕГРН, в договоре и на публичной кадастровой "
        "карте nspd.gov.ru/map. Мы доделаем отчёт вручную и БЕЗ доплаты.\n\n"
        "Если номера нет — пришлите полный адрес с корпусом и номером квартиры, найдём сами.\n\n"
        "Та часть проверки, которая уже выполнена, доступна здесь: %s\n\nЧистаяСделка"
    ) % (ref or "—", url)
    html = (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;font-size:15px;'
        'line-height:1.6;color:#1f2430;max-width:560px;margin:0 auto;padding:24px 20px">'
        '<p>Здравствуйте!</p>'
        '<p>Оплата получена, проверку мы запустили. По объекту, который вы указали '
        f'(<b>{escape(ref) or "—"}</b>), сведения в реестрах не нашлись — такое бывает, когда '
        'адрес записан иначе, чем в ЕГРН, или когда вместо кадастрового номера указан условный.</p>'
        '<p><b>Пришлите, пожалуйста, в ответ на это письмо кадастровый номер объекта</b> — вида '
        '77:01:0004012:1122. Он есть в выписке ЕГРН, в договоре и на '
        '<a href="https://nspd.gov.ru/map" style="color:#2563eb">публичной кадастровой карте</a>. '
        'Мы доделаем отчёт вручную и <b>без доплаты</b>.</p>'
        '<p>Если номера нет — пришлите полный адрес с корпусом и номером квартиры, найдём сами.</p>'
        f'<p style="margin:22px 0"><a href="{url}" style="display:inline-block;background:#7a1815;'
        'color:#fff;text-decoration:none;padding:13px 22px;border-radius:9px;font-weight:600">'
        'Открыть выполненную часть</a></p>'
        '<p style="margin-top:26px;padding-top:16px;border-top:1px solid #e7e9ee;font-size:13px;'
        'color:#6b7280">ЧистаяСделка · chistasdelka.ru<br>Отвечайте прямо на это письмо.</p></div>'
    )
    try:
        from modules import mailer
        r = mailer.send_email(
            to=email, subject="Нужен кадастровый номер — доделаем отчёт без доплаты",
            html=html, text=text,
            from_email=os.getenv("EMAIL_REPLY_TO", "support@chistasdelka.ru"),
            from_name="ЧистаяСделка",
            reply_to=os.getenv("EMAIL_REPLY_TO", "support@chistasdelka.ru"),
            tag="object_missing")
        if isinstance(r, dict) and r.get("status") == "success":
            rec["ready_email_sent"] = True
            # Ход перешёл к клиенту. Этот же флаг читает сторож (adminbot.undelivered)
            # и перестаёт числить заказ за нами — раньше его никто не выставлял, и
            # отработанные письма висели в тревогах как незакрытые дела.
            rec["ask_sent"] = datetime.utcnow().isoformat() + "Z"
            _save_report_record(rec)
    except Exception as e:  # noqa: BLE001 — письмо не имеет права ронять платёжный флоу
        print(f"[object_missing] письмо не ушло: {type(e).__name__}: {e}", flush=True)
    try:
        from modules import adminbot
        adminbot.notify(
            "Объект не найден по оплаченному заказу\n\nОтчёт: %s\nВвод клиента: %s\nПочта: %s\n\n"
            "Клиенту отправлена просьба прислать кадастровый номер. Доделать вручную." % (
                report_id, ref or "—", email))
    except Exception as e:  # noqa: BLE001
        print(f"[object_missing] алерт не ушёл: {type(e).__name__}: {e}", flush=True)


def _send_report_ready_email(email: str, report_id: str) -> None:
    """Письмо «отчёт готов» со ссылкой на страницу отчёта. Идемпотентно (одно
    письмо на отчёт, флаг ready_email_sent). НИКОГДА не роняет платёжный флоу."""
    if not email or not report_id or not os.getenv("UNISENDER_GO_API_KEY"):
        return
    rec = _load_report(report_id)
    if not rec or rec.get("ready_email_sent"):
        return
    if rec.get("preview"):
        return  # отчёт ещё не финализирован (сбой проверок) — письмо «готов» не шлём
    # Объект не нашёлся и в платных базах: отчёт формально готов (продавец проверен),
    # но главного в нём нет. Слать бодрое «ваш отчёт готов» на такое — обман, а молчать
    # значит потерять человека. Пишем прямо: нужен точный номер, доделаем без доплаты.
    if rec.get("object_missing"):
        _send_object_missing_email(email, report_id, rec)
        return
    try:
        from modules import mailer
        base = os.getenv("PUBLIC_BASE", "https://chistasdelka.ru").rstrip("/")
        url = f"{base}/report/{report_id}/view"
        tpl = (STATIC_DIR / "emails" / "report_ready.html").read_text(encoding="utf-8")
        # Прикладываем PDF к письму. Ссылка остаётся, но полагаться только на неё
        # нельзя: за 4 августа трое оплативших написали «не открывается» — все с
        # телефонов, из встроенных браузеров Gmail и Mail.ru. Вложение доходит
        # независимо от браузера, и человеку не нужно никуда переходить.
        pdf = REPORTS_DIR / f"{report_id}.pdf"
        att = [(f"chistaya-sdelka-{report_id}.pdf", str(pdf))] if pdf.exists() else None
        r = mailer.send_email(
            to=email, subject="Ваш отчёт готов", html=tpl.replace("__URL__", url),
            from_email=os.getenv("EMAIL_FROM", "noreply@chistasdelka.ru"),
            from_name="ЧистаяСделка",
            reply_to=os.getenv("EMAIL_REPLY_TO", "support@chistasdelka.ru"),
            text="Ваш отчёт готов. Он приложен к письму, а также открывается по ссылке: " + url,
            tag="report_ready", attachments=att,
        )
        if isinstance(r, dict) and r.get("status") == "success":
            rec["ready_email_sent"] = True
            _save_report_record(rec)
    except Exception:
        pass


def _record_paid_order(order: dict[str, Any], report_id: str) -> None:
    """Записать успешную оплату в реестр + offline-конверсию в Метрику. Идемпотентно."""
    if order.get("paid_recorded"):
        return
    email, ym_uid = "", ""
    if report_id:
        r = _load_report(report_id)
        if r:
            email = r.get("email", "") or ""
            ym_uid = r.get("ym_uid", "") or ""
    orders.record_payment(order, email)  # ставит paid_recorded
    # Финализация — ТОЛЬКО для ПЕРВИЧНОГО заказа отчёта (base/object). Апселлы (addon/
    # addon_seller/bump/monitor) не финализируют — их применяют _maybe_trigger_*.
    # object_only берём из ТАРИФА заказа (#4/#8), а не из мутабельного rec-флага.
    otar = (order.get("tariff") or "base")
    # upgrade — доплата с базового тарифа до полного. Отчёт пересобирается целиком
    # с object_only=False: заново дёргаем источник и открываем блоки, которые в
    # базовом были закрыты. Дороже, чем «снять замок с уже полученного», но
    # честнее — человек получает свежие данные на момент доплаты, а не вчерашние.
    if otar == "recheck":
        import threading
        threading.Thread(target=_apply_recheck, args=(report_id,), daemon=True).start()
    if otar == "upgrade":
        # Базовый отчёт уже финализирован, и финализатор по флагу preview вышел бы
        # сразу — доплата списалась бы, а блоки не открылись. Возвращаем отчёт в
        # состояние «собирается»: это и технически верно, и честно на экране.
        _r = _load_report(report_id)
        if _r:
            _r["preview"] = True
            for _k in ("finalize_started_at", "finalize_attempts", "finalize_permanent",
                       "ready_email_sent", "ask_sent"):
                _r.pop(_k, None)
            _save_report_record(_r)
    if otar in ("base", "object", "ext", "realtor", "upgrade"):
        import threading
        threading.Thread(target=_finalize_then_notify,
                         args=(report_id, email, otar == "object"), daemon=True).start()
    # Апселлы запускаем ЗДЕСЬ, под защитой paid_recorded. Раньше они висели в
    # обработчике GET /pay/return, то есть каждое открытие страницы (F5, «назад»,
    # ссылка из истории) было платным действием. Здесь путь один и однократный:
    # первым пришёл webhook или return — неважно, второй уже не дойдёт.
    if report_id:
        _maybe_trigger_addon(order, report_id)
        _maybe_trigger_seller_addon(order, report_id)
        _maybe_trigger_bump(order, report_id)
    # Offline-конверсия pay_success по ClientId — чинит атрибуцию к рекламе
    # (клиентский pay_success после ЮKassa уходит в «прямой заход»).
    if ym_uid and order.get("amount"):
        try:
            from modules import metrika
            metrika.upload_pay_conversion(ym_uid, order["amount"])
        except Exception as exc:  # noqa: BLE001
            print(f"[metrika] сбой загрузки конверсии: {type(exc).__name__}: {exc}",
                  flush=True)
    else:
        # Оплата без ClientId — реклама её не увидит. Обычно это платёж по
        # прямой ссылке мимо браузера; если такое пойдёт валом, атрибуция врёт.
        print(f"[metrika] конверсия пропущена: ClientId={'есть' if ym_uid else 'НЕТ'}, "
              f"сумма={order.get('amount')}", flush=True)


import threading as _threading
_finalize_lock = _threading.Lock()
_finalizing: set[str] = set()
# #2: общий лок для атомарного check-then-set pending-флагов апселлов (webhook+return гонка).
_trigger_lock = _threading.Lock()


def _finalize_paid_report(report_id: str, object_only: bool | None = None) -> bool:
    """После оплаты: догнать полную проверку (ФССП + платные базы) + LLM-вердикт + PDF.
    Идемпотентно (флаг preview) + АТОМАРНЫЙ захват: webhook и pay/return могут прийти
    одновременно — без захвата оба запустили бы платные проверки NewDB (двойное списание).
    object_only: что финализировать — берётся из ОПЛАЧЕННОГО заказа (не из мутабельного
    rec-флага), иначе две вкладки (base+object) на один report_id → финализация не того
    продукта. None → фолбэк на rec['object_only'] (для startup-ретрая без заказа-контекста).
    Возвращает True, если отчёт финализирован (preview=False)."""
    with _finalize_lock:
        rec0 = _load_report(report_id)
        if not rec0:
            return False
        if not rec0.get("preview"):
            return True  # уже финализирован
        if report_id in _finalizing:
            return False  # уже финализируется в другом потоке — не задваиваем платные вызовы
        _finalizing.add(report_id)
        # Отметка старта нужна странице отчёта: она показывает «занимает дольше
        # обычного» по ЭТОМУ времени, а не по времени загрузки вкладки — иначе
        # F5 обнулял бы отсчёт ровно у того, кто уже устал ждать.
        if not rec0.get("finalize_started_at"):
            rec0["finalize_started_at"] = datetime.utcnow().isoformat() + "Z"
            _save_report_record(rec0)
    try:
        return _do_finalize(report_id, object_only=object_only)
    finally:
        with _finalize_lock:
            _finalizing.discard(report_id)


def _do_finalize(report_id: str, object_only: bool | None = None) -> bool:
    rec = _load_report(report_id)
    if not rec:
        return False
    if not rec.get("preview"):
        return True  # уже финализирован
    # #4/#8: что финализировать определяет ОПЛАЧЕННЫЙ заказ (передан явно), а не живой флаг.
    if object_only is None:
        object_only = bool(rec.get("object_only"))
    else:
        rec["object_only"] = object_only  # синхронизируем флаг с реально оплаченным продуктом
    try:
        # Если объект уже опознан в превью — идём в платные базы по КАДАСТРУ, а не по
        # тому, что человек набрал. Иначе выходило глупо: превью потратило платный
        # запрос, точно определило квартиру (в т.ч. автоподбором из списка помещений
        # дома) и показало её характеристики — а финализация начинала поиск заново по
        # исходному адресу и нередко его не находила. Итог: «объекта нет в ЕГРН» и
        # письмо «пришлите кадастровый номер» при том, что номер у нас на руках.
        # По номеру источники находят почти всегда, по адресу — примерно в половине.
        # НО не когда в превью лежит ДОМ вместо квартиры (house_only): там кадастр
        # здания, и проверка по нему выдала бы отчёт про весь дом. В этом случае
        # платный путь должен искать по адресу — он видит больше НСПД и квартиру
        # ещё может найти.
        ref = ("" if _preview_is_house_not_flat(rec) else _resolved_cadastre(rec)) \
            or rec.get("object_ref") or "—"
        if ref != (rec.get("object_ref") or ""):
            print(f"[finalize] объект по кадастру из превью: {ref} "
                  f"(ввод был {str(rec.get('object_ref'))[:48]!r})", flush=True)
        req = CheckRequest(
            seller_name=rec.get("seller_name") or "—",
            seller_dob=rec.get("seller_dob") or "",
            object_ref=ref,
            seller_inn=rec.get("seller_inn") or "",
            email=(rec.get("email") or None),
            tariff=rec.get("tariff", "base"), consent=True,
        )
        checks = _run_checks(req, preview=False, object_only=object_only)  # единый прогон без ретраев

        # Схемы повреждений по ДТП забираем к себе. Ссылка ведёт на CDN
        # поставщика: она может протухнуть, а до тех пор каждый открывший отчёт
        # светит ему свой браузер. Картинка маленькая, скачивание бесплатно.
        try:
            _save_dtp_schemes(report_id, checks)
        except Exception as exc:  # noqa: BLE001 — картинка не стоит отчёта
            print(f"[scheme] {report_id}: {type(exc).__name__}: {exc}", flush=True)

        # Паспорт машины и периоды владения приезжают тем же вызовом gai, что и
        # ограничения — отдельных денег не стоят. _run_checks кладёт их сюда.
        extra = getattr(_auto_extra, "value", None) or {}
        for _k in ("passport", "owners", "vin_check", "facts"):
            if extra.get(_k):
                rec[_k] = extra[_k]

        # ГЕЙТ ВЫДАЧИ — АВТОМОБИЛЬНЫЙ. Ниже по функции остался гейт недвижимости:
        # он ищет блок с ключом «object» и, не найдя, выбрасывает ВЕСЬ отчёт. У
        # машины таких ключей нет вовсе (restrict/pledge/wanted/...), поэтому
        # падала КАЖДАЯ платная сборка: 50 ₽ поставщику мы платили, отчёт
        # выбрасывали, клиент не получал ничего. Проверено 08.08.2026 живым
        # прогоном — до него платный путь ни разу не гоняли целиком.
        #
        # Правило простое: отдаём, если хоть один блок реально проверен.
        # Молчание ВСЕХ источников — единственный случай, когда отдавать нечего;
        # он же единственный, где ретрай осмыслен.
        real = [c for c in checks if c.status in ("found", "not_found")]
        if not real:
            rec["finalize_failed"] = datetime.utcnow().isoformat() + "Z"
            rec["object_missing"] = True
            _save_report_record(rec)
            _bump_metric("finalize_failed")
            print(f"[ALERT] finalize авто: источник молчит по всем блокам "
                  f"report={report_id} ref={str(rec.get('object_ref'))[:24]!r}", flush=True)
            return False
        # Часть блоков могла не ответить — это нормально и честно отражено в отчёте
        # («не проверено» + причина). Отчёт отдаём: остальные проверки оплачены.
        if len(real) < len([c for c in checks if c.status != "locked"]):
            print(f"[partial] авто: проверено {len(real)} блоков из "
                  f"{len([c for c in checks if c.status != 'locked'])} report={report_id}", flush=True)
            _bump_metric("partial_auto")
        rec.pop("object_missing", None)

        # Тип объекта («Квартира», «Здание», «Земельный участок») — из превью НСПД:
        # в детали блока ЕГРН лежит только назначение («жилое»), и по нему квартиру
        # от здания не отличить.
        verdict = build_verdict(checks,
                                seller_name=(req.seller_name if not object_only else ""),
                                object_kind=str((rec.get("object_preview") or {}).get("type") or ""))
        recs = list(verdict.recommendations)
        body = verdict.body
        if rec.get("object_unresolved"):
            # Просьба уточнить номер — первой строкой, чтобы не потерялась.
            ask = ("Уточните, пожалуйста, кадастровый номер объекта — по указанному "
                   f"({rec.get('object_ref', '')}) сведения не найдены. Пришлите верный номер "
                   "в ответ на это письмо, и мы проверим объект без доплаты. "
                   "Результаты проверки продавца по ФИО — ниже.")
            recs.insert(0, ask)
            body = ask + ("\n\n" + body if body else "")
            # PDF собирается из объекта verdict, а не из rec — иначе просьба
            # уточнить номер была бы только на сайте, но не в скачанном файле.
            try:
                verdict.body = body
                verdict.recommendations = recs
            except Exception:  # noqa: BLE001 — не дать этому уронить выдачу отчёта
                pass
        rec.update({"risk": verdict.risk, "headline": verdict.headline, "body": body,
                    "recommendations": recs, "llm_used": verdict.llm_used,
                    "preview": False, "checks": [_serialize_check(c) for c in checks]})
        rec.pop("finalize_failed", None)
        try:
            # Паспорт, владельцы и схемы повреждений — в PDF тоже. Он у нас
            # продаётся как документ ДЛЯ СДЕЛКИ, и отдавать в нём меньше, чем
            # человек уже видел на странице, — обман ожиданий.
            _schemes = [str(SCHEMES_DIR / f"{report_id}-{i}.png")
                        for i in range(1, 6)
                        if (SCHEMES_DIR / f"{report_id}-{i}.png").exists()]
            render_report(str(REPORTS_DIR / f"{report_id}.pdf"), report_id=report_id,
                          seller_name=req.seller_name, seller_dob=req.seller_dob,
                          object_ref=req.object_ref, email=req.email or "",
                          checks=checks, verdict=verdict,
                          deal_kit=_deal_kit_payload(report_id, rec),
                          passport=rec.get("passport") or {},
                          owners=rec.get("owners") or [],
                          schemes=_schemes)
        except Exception:  # noqa: BLE001
            pass  # PDF не критичен — веб-версия отчёта доступна
        _save_report_record(rec)
        return True
    except Exception as e:  # noqa: BLE001
        rec["finalize_failed"] = datetime.utcnow().isoformat() + "Z"
        _save_report_record(rec)
        print(f"[ALERT] finalize failed report={report_id}: {e}", flush=True)
        return False


def _bump_metric(name: str) -> None:
    """Инкремент счётчика в counters (алерты финализации/почты) — best-effort."""
    try:
        abstats.bump_counter(orders.PAYMENTS_LOG.parent, name)
    except Exception:  # noqa: BLE001
        pass


def _finalize_then_notify(report_id: str, email: str, object_only: bool | None = None) -> None:
    """Фоново: полная проверка после оплаты. Письмо «отчёт готов» — ТОЛЬКО если реально
    финализировано (иначе клиент получил бы письмо о пустом отчёте). object_only — из
    тарифа оплаченного заказа (что финализировать)."""
    if _finalize_paid_report(report_id, object_only=object_only):
        # #2 (6-й прогон): реконсиляция «base-платёж поверх object-отчёта». Два pending-заказа
        # (object+base) на один report_id могли быть оплачены оба: первый финализировал
        # object-only, второй (base, полная цена) вернул «уже финализирован» и раньше молча
        # проглатывался — продавец не проверялся. Теперь дотриггериваем проверку продавца
        # (ФИО сохранено в rec при base-оплате) как апселл-апгрейд.
        if object_only is False:
            rec2 = _load_report(report_id)
            if (rec2 and rec2.get("object_only") and not rec2.get("seller_added")
                    and rec2.get("seller_name")):
                print(f"[reconcile] base-платёж поверх object-отчёта → дотриггер продавца report={report_id}", flush=True)
                _maybe_trigger_seller_addon({"addon_seller": 1}, report_id)
        elif object_only is True:
            rec2 = _load_report(report_id)
            if rec2 and not rec2.get("object_only"):
                # object-платёж поверх уже ПОЛНОГО отчёта: услуга уже оказана шире оплаченной,
                # но деньги взяты повторно — алерт оператору на ручной возврат.
                _bump_metric("duplicate_object_payment")
                print(f"[ALERT] object-платёж поверх полного отчёта (вернуть?) report={report_id}", flush=True)
        try:
            _send_report_ready_email(email, report_id)
        except Exception:  # noqa: BLE001
            pass
    else:
        _bump_metric("report_ready_email_skipped")  # алерт: оплата есть, отчёт не готов — нужен ретрай


# ────────────────────────────────────────────────────────────────────────────
# Слоты платных апселлов: (флаг «сделано», флаг «идёт», счётчик попыток).
#
# Гарды апселлов смотрели только на «сделано» и «идёт». При НЕУДАЧЕ _apply_*
# снимает «идёт», а «сделано» не ставит (проверки-то не было) — то есть оба
# флага гарда оказывались сняты, и следующий вызов снова платил. А вызывались
# они на КАЖДОМ GET /pay/return: F5, «назад», ссылка из истории. Провайдер
# тормозит → клиент видит «не получилось» → жмёт F5 → мы платим до 5 запросов
# и снова падаем. Потолка не было никакого.
#
# Теперь неудача расходует попытку. Три исчерпаны — дальше руками через админку,
# деньги не жжём.
# ────────────────────────────────────────────────────────────────────────────
_ADDON_MAX_ATTEMPTS = 3
_ADDON_SLOTS: dict[str, tuple[str, str, str]] = {
    "addon":  ("addon_applied",  "addon_pending",        "addon_attempts"),
    "seller": ("seller_added",   "seller_addon_pending", "seller_addon_attempts"),
    "bump":   ("bump_applied",   "bump_pending",         "bump_attempts"),
}


def _claim_addon_slot(report_id: str, kind: str) -> bool:
    """Атомарно занять слот на платный прогон апселла. False — не запускать.

    Атомарность (как в _finalize_paid_report): webhook и pay/return приходят
    одновременно, без лока оба прочли бы pending=False и заплатили дважды."""
    done_f, pending_f, att_f = _ADDON_SLOTS[kind]
    with _trigger_lock:
        rec = _load_report(report_id)
        if not rec or rec.get(done_f) or rec.get(pending_f):
            return False
        n = int(rec.get(att_f) or 0)
        if n >= _ADDON_MAX_ATTEMPTS:
            print(f"[addon] {kind} report={report_id}: попытки исчерпаны ({n}) — "
                  f"платный прогон НЕ запущен, нужен разбор руками", flush=True)
            _bump_metric(f"addon_attempts_exhausted_{kind}")
            return False
        rec[pending_f] = True
        rec[att_f] = n + 1
        _save_report_record(rec)
    return True


def _is_addon_order(order: dict[str, Any]) -> bool:
    """Заказ доп-проверки банкротства. Раньше признаком служил заполненный ИНН —
    и когда поле ИНН убрали из интерфейса (27.07), запуск проверки молча
    перестал срабатывать: деньги брали, проверку не делали. Признак теперь —
    тариф; ИНН необязателен, поиск по Федресурсу идёт по ФИО."""
    return str(order.get("tariff") or "") == "addon" or bool(order.get("addon_inn"))


def _maybe_trigger_addon(order: dict[str, Any], report_id: str) -> None:
    """Для addon-заказа запустить доп-проверку в фоне (идемпотентно, ≤3 попыток)."""
    if _is_addon_order(order) and report_id and _claim_addon_slot(report_id, "addon"):
        import threading
        threading.Thread(target=_apply_addon,
                         args=(report_id, str(order.get("addon_inn") or "")), daemon=True).start()


def _maybe_trigger_seller_addon(order: dict[str, Any], report_id: str) -> None:
    """Для addon_seller-заказа запустить проверку продавца и мерж в отчёт (идемпотентно).
    Вызывается из ВСЕХ путей подтверждения оплаты (bypass-return, real-return, webhook) —
    иначе деньги за апселл взяты, а продавец не проверен (баг C1)."""
    if order.get("addon_seller") and report_id and _claim_addon_slot(report_id, "seller"):
        import threading
        threading.Thread(target=_apply_seller_addon, args=(report_id,), daemon=True).start()


def _apply_bump(report_id: str) -> None:
    """Order bump (BUMP_PRICE): проверка залогов движимого имущества (реестр ФНП)
    по ФИО — добавляется в ОСНОВНОЙ список проверок, вердикт и PDF пересобираются."""
    # #1 (гонка): дожидаемся ФИНАЛИЗАЦИИ базового отчёта (preview=False), прежде чем
    # читать/дописывать rec["checks"]. Иначе _do_finalize полной заменой checks затрёт
    # наши залоги (или мы затрём его реальные проверки плейсхолдерами). Оба потока стартуют
    # с одного подтверждения оплаты; ждём, чтобы наш мерж лёг ПОВЕРХ финализированного списка.
    import time
    for _ in range(150):  # до ~150с (реальные проверки идут 50-90с; 60с было впритык)
        rec0 = _load_report(report_id)
        if not rec0:
            return
        if not rec0.get("preview"):
            break
        time.sleep(1)
    rec = _load_report(report_id)
    if not rec:
        return
    # #1 (6-й прогон): финализация НЕ завершилась (таймаут/сбой баланса) → НЕ применяем bump
    # в preview-плейсхолдеры (там уже есть key='pledges'-плейсхолдер — реальный результат
    # молча выбросился бы, bump_applied=True заблокировал бы ретрай навсегда). Оставляем
    # bump_pending=True — startup-скан (_revive) перезапустит после успешной финализации.
    if rec.get("preview"):
        print(f"[ALERT] bump: финализация не завершилась, откладываем на ретрай report={report_id}", flush=True)
        return
    try:
        from modules import premium
        pl = premium.pledges(rec.get("seller_name", ""))
        pl_status, pl_detail = pl.status, pl.detail
        pl_items = getattr(pl, "items", []) or []
    except Exception:
        # Источник недоступен — НЕ теряем оплаченный бамп: добавляем честный
        # «не проверено» (клиент получил расширенный отчёт, залоги перепроверит).
        pl_status, pl_detail = "not_checked", "Реестр залогов ФНП временно недоступен — повторите проверку позже."
        pl_items = []
    checks = [Check(key=c["key"], name=c["name"], source=c["source"],
                    status=c["status"], detail=c["detail"], items=c.get("items", []))
              for c in rec.get("checks", [])]
    # финализированный отчёт base не содержит pledges (только ext) — заменяем и плейсхолдер
    checks = [c for c in checks if c.key != "pledges" or c.status != "pending"]
    if not any(c.key == "pledges" for c in checks):
        checks.append(Check(key="pledges", name="Залоги и обременения (движимое имущество)",
                            source="Реестр залогов ФНП", status=pl_status,
                            detail=pl_detail, items=pl_items))
    # Учитываем находки оплаченной ИНН-допроверки (addon хранится отдельным
    # блоком, не в rec["checks"]) — иначе пересчёт риска «забудет» банкротство/
    # арбитраж и гейдж откатится в «низкий» после покупки бампа.
    risk_checks = list(checks)
    for c in ((rec.get("addon_result") or {}).get("checks") or []):
        risk_checks.append(Check(key=c.get("key", ""), name=c.get("name", ""),
                                 source=c.get("source", ""), status=c.get("status", ""),
                                 detail=c.get("detail", ""), items=c.get("items", []) or []))
    from modules.verdict import compute_risk, _rule_headline
    rec["risk"] = compute_risk(risk_checks)
    rec["headline"] = _rule_headline(rec["risk"])
    rec["checks"] = [_serialize_check(c) for c in checks]
    rec["bump_pending"], rec["bump_applied"] = False, True
    # PDF пересобираем с расширенным списком проверок.
    try:
        from modules.verdict import Verdict as _Verdict
        pdf_verdict = _Verdict(risk=rec["risk"], headline=rec["headline"],
                               body=rec.get("body", ""),
                               recommendations=rec.get("recommendations", []), llm_used=True)
        render_report(str(REPORTS_DIR / f"{report_id}.pdf"), report_id=report_id,
                      seller_name=rec.get("seller_name", ""), seller_dob=rec.get("seller_dob", ""),
                      object_ref=rec.get("object_ref", ""), email=rec.get("email", "") or "",
                      checks=checks, verdict=pdf_verdict, addon=rec.get("addon_result"),
                      deal_kit=_deal_kit_payload(report_id, rec))
    except Exception:
        pass
    _save_report_record(rec)


def _maybe_trigger_bump(order: dict[str, Any], report_id: str) -> None:
    """Для base-заказа с bump=True запустить проверку залогов в фоне (идемпотентно, ≤3 попыток)."""
    if order.get("bump") and report_id and _claim_addon_slot(report_id, "bump"):
        import threading
        threading.Thread(target=_apply_bump, args=(report_id,), daemon=True).start()


@app.get("/report/sample.pdf")
def api_sample_report() -> FileResponse:
    """Демонстрационный образец отчёта (вымышленные данные) — снимает страх
    «заплачу и получу пустышку», показывает реальный формат результата."""
    sample_path = REPORTS_DIR / "sample.pdf"
    src = STATIC_DIR / "example_report.json"
    # Собираем из ТОГО ЖЕ примера, что показан на /example. Раньше здесь лежал
    # выдуманный продавец недвижимости с кадастровым номером и долгом у
    # приставов — под автомобильной шапкой. Это был первый файл, который
    # скачивал человек, решающий, платить ли нам.
    if src.exists() and (not sample_path.exists()
                         or sample_path.stat().st_mtime < src.stat().st_mtime):
        d = json.loads(src.read_text(encoding="utf-8"))
        checks = [Check(key=c.get("key", ""), name=c.get("name", ""),
                        source=c.get("source", ""), status=c.get("status", ""),
                        detail=c.get("detail", ""), items=c.get("items") or [])
                  for c in (d.get("checks") or [])]
        verdict = Verdict(risk=d.get("risk", ""), headline=d.get("headline", ""),
                          body=d.get("body", ""),
                          recommendations=d.get("recommendations") or [],
                          llm_used=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        schemes = [str(SCHEMES_DIR / "example-1.png")] if (SCHEMES_DIR / "example-1.png").exists() else []
        render_report(
            str(sample_path), report_id="ОБРАЗЕЦ",
            seller_name="", seller_dob="",
            object_ref=d.get("vin") or "", email="",
            checks=checks, verdict=verdict,
            passport=d.get("passport") or {}, owners=d.get("owners") or [],
            schemes=schemes,
        )
    if not sample_path.exists():
        raise HTTPException(status_code=404, detail="Образец пока не подготовлен.")
    return FileResponse(str(sample_path), media_type="application/pdf",
                        headers={"Content-Disposition": "inline; filename=obrazec-otcheta.pdf"})


@app.get("/api/report/{report_id}")
def api_report(report_id: str) -> FileResponse:
    rec = REPORTS.get(report_id)
    if not rec:
        # try loading from disk (survive restarts)
        j = REPORTS_DIR / f"{report_id}.json"
        p = REPORTS_DIR / f"{report_id}.pdf"
        if j.exists() and p.exists():
            try:
                REPORTS[report_id] = json.loads(j.read_text(encoding="utf-8"))
                rec = REPORTS[report_id]
            except Exception:
                pass
    if not rec:
        raise HTTPException(status_code=404, detail="report not found")

    # PDF-отчёт — платный: отдаём только по оплаченному заказу (freemium).
    if not orders.is_report_paid(report_id):
        raise HTTPException(status_code=402, detail="Отчёт доступен после оплаты.")

    pdf_path = Path(rec.get("pdf_path") or (REPORTS_DIR / f"{report_id}.pdf"))
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="pdf missing")

    # inline, а не attachment. Встроенные браузеры почтовых приложений (Gmail, Mail.ru)
    # часто просто НЕ выполняют принудительное скачивание: открывается пустая вкладка,
    # файл не приходит, человек пишет «по ссылке невозможно скачать отчёт» — так и было
    # с отчётом 1bc9a30808d7. С inline PDF открывается штатной смотрелкой, откуда его
    # можно сохранить или отправить. На десктопе сохранение обеспечивает атрибут
    # download у кнопки в static/report.html — имя файла берётся оттуда же.
    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename=f"chistaya-sdelka-{report_id}.pdf",
        content_disposition_type="inline",
    )


def _app_base(request: Request) -> str:
    return os.getenv("APP_BASE_URL", "").strip() or str(request.base_url).rstrip("/")


@app.post("/api/lead")
async def api_lead(request: Request) -> JSONResponse:
    """Ранний lead-capture: email вводится на шаге 1 мастера, ДО оплаты. Сохраняем,
    чтобы можно было дожать тех, кто бросил мастер на середине. Минимум ПДн (email
    заказчика + объект). #10: согласие — действием (под кнопкой «Далее» шага email
    стоит юр-подпись со ссылкой на Политику), фиксируем consent_implied в записи."""
    from datetime import timezone
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        b = {}
    email = str((b or {}).get("email", "")).strip()[:120]
    if not email or "@" not in email:
        return JSONResponse({"ok": True})   # мусор молча игнорим — не мешаем UX мастера
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "email": email,
           "report_id": str((b or {}).get("report_id", ""))[:40],
           "object_ref": str((b or {}).get("object_ref", ""))[:120],
           "ip": _client_ip(request),
           "consent_implied": True}  # #10: согласие действием (юр-подпись под «Далее» шага email)
    try:
        lf = orders.PAYMENTS_LOG.parent / "leads.jsonl"
        lf.parent.mkdir(parents=True, exist_ok=True)
        with lf.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — lead-capture не должен ронять шаг мастера
        pass
    return JSONResponse({"ok": True})


@app.post("/api/pay")
def api_pay(req: PayRequest, request: Request) -> JSONResponse:
    rec = _load_report(req.report_id)  # с диск-фолбэком: оплата не ломается после рестарта/деплоя
    if not rec:
        raise HTTPException(status_code=404, detail="report not found")

    # #2: отчёт УЖЕ оплачен → не берём деньги повторно (bfcache-Back после оплаты, вторая
    # вкладка). Для добавления продавца к оплаченному объекту есть отдельный /api/addon-seller.
    if orders.is_report_paid(req.report_id):
        raise HTTPException(status_code=409, detail=(
            "Этот отчёт уже оплачен. Откройте его по ссылке из письма. "
            "Чтобы добавить проверку продавца к отчёту по объекту — используйте кнопку в самом отчёте."))

    # #4: любой НЕ-object платёж (base/ext/realtor) чистит залипший object_only, иначе
    # _do_finalize прогонит только объект, а списана полная цена. (Флаг мог остаться от
    # брошенной object-оплаты на том же report_id.)
    if not req.object_only and rec.get("object_only"):
        rec["object_only"] = False
        _save_report_record(rec)

    # #8: тарифы ext/realtor (2990/9900) выведены из модели раунда 3 (с лендинга убраны,
    # противоречат оферте 249-899). Не принимаем — только object / base(полный).
    if not req.object_only and req.tariff not in ("base",):
        raise HTTPException(status_code=400,
                            detail="Этот тариф недоступен. Доступны: проверка объекта или полный отчёт (объект и продавец).")

    # object-only: проверка ТОЛЬКО объекта, без ФИО продавца. Не требуем данные продавца,
    # помечаем отчёт — финализация прогонит только ЕГРН (обременения/собственники/история).
    if req.object_only:
        # Раньше здесь стоял отказ, если бесплатное превью не опознало объект.
        # Но молчание бесплатного источника не значит, что объекта нет: сама проверка
        # идёт по платным государственным базам, где сведений больше. Отказывая, мы
        # теряли весь этот поток. Теперь платёж проходит, проверка запускается, и если
        # объект не найдётся И там — клиенту уходит письмо с просьбой прислать точный
        # номер, а отчёт доделывается вручную без доплаты (_send_object_missing_email).
        # Просим именно письмом, а не кнопкой «изменить объект»: иначе человек начнёт
        # перебирать адреса, а каждый перебор — наш платный запрос.
        # Согласие на обработку ПДн обязательно и здесь: отчёт по объекту раскрывает
        # собственников/доли (ПДн третьих лиц из ЕГРН). Гейт как у других платных входов.
        if not req.consent:
            raise HTTPException(status_code=400,
                                detail="Требуется согласие на обработку персональных данных (152-ФЗ).")
        # Новые территории: объектный тариф — это ровно то, что мы выполнить не можем.
        # Фронт такую кнопку не показывает, но обойти его ничего не стоит, а деньги
        # берутся здесь. Отказываем деньгами, а не письмом с извинениями потом.
        # Берём флаг, посчитанный в превью: он учитывает и адресный ввод (там округ
        # виден только по коду региона из DaData, в object_ref его нет).
        _blocked = (str(rec.get("no_egrn_district") or "")
                    or _no_egrn_district(str(rec.get("object_ref") or "")))
        if _blocked:
            raise HTTPException(status_code=400, detail=(
                f"Объект в {_blocked} кадастровом округе: по новым регионам Росреестр пока "
                f"не публикует сведения ЕГРН, и проверить объект мы не сможем. "
                f"Доступна полная проверка — по продавцу."))
        rec["object_only"] = True
        # #20/#22: фиксируем факт согласия (152-ФЗ) в отчёте — доказательная база для РКН,
        # как у полных отчётов (там consent=True сохраняется в seller-ветке).
        _record_consent(rec, request, req.consent_text, req.consent_version)
        if req.email:  # email нужен для чека 54-ФЗ и письма «отчёт готов»
            rec["email"] = str(req.email)
        _save_report_record(rec)

    # #13: email обязателен на КАЖДОМ платном входе — иначе ЮKassa не пришлёт чек 54-ФЗ и
    # некуда слать «отчёт готов». Берём из запроса или ранее сохранённого в отчёте.
    if req.email:
        rec["email"] = str(req.email)
    if not (rec.get("email") and "@" in str(rec.get("email"))):
        raise HTTPException(status_code=400, detail="Укажите email — на него придёт отчёт и чек.")

    # Данные продавца приходят на шаге оплаты (новая воронка: превью по объекту без ПДн →
    # оплата с ПДн). Сохраняем в отчёт — финализация после оплаты проверит продавца по
    # ФССП/ЕФРСБ/арбитражу. Раньше их требовали ДО бесплатного превью → теряли 76% на форме.
    if req.seller_name and not req.object_only:
        if not req.consent:
            raise HTTPException(status_code=400,
                                detail="Требуется согласие на обработку персональных данных продавца (152-ФЗ).")
        fio = " ".join(req.seller_name.split())
        if len(fio.split()) < 2:
            raise HTTPException(status_code=400, detail="Укажите ФИО продавца полностью (фамилия и имя).")
        rec["seller_name"] = fio[:120]
        if req.seller_dob:
            rec["seller_dob"] = req.seller_dob.strip()[:10]
        if req.email:
            rec["email"] = str(req.email)
        if req.passport_seria:
            rec["passport_seria"] = req.passport_seria.strip()[:4]
        if req.passport_nomer:
            rec["passport_nomer"] = req.passport_nomer.strip()[:6]
        _record_consent(rec, request, req.consent_text, req.consent_version)
        _save_report_record(rec)

    # Оплату НЕ блокируем даже при лежащем источнике: клиент дошёл до кнопки
    # через шесть шагов и второй раз не придёт, а отчёт мы довезём ретраем.
    if _provider_degraded():
        rec["provider_degraded_at"] = datetime.utcnow().isoformat() + "Z"
        _save_report_record(rec)

    tariff_key = req.tariff if req.tariff in TARIFFS else "base"
    amount = TARIFFS[tariff_key]["amount"]
    # A/B/n: цена по варианту из куки. #9: если куки нет (блокировщик/API-вызов) — списываем
    # по B, но в заказ пишем variant="" (вне A/B-выборки), чтобы не смещать плечо B.
    _cookie_v = _get_variant(request)
    variant = _cookie_v or "B"
    order_variant = _cookie_v or ""  # "" → не зачисляется ни в одно плечо A/B-панелей
    amount_override = None
    # Допродажи недвижимости (bump «залоги ФНП по ФИО продавца» и «пакет к сделке
    # по квартире») на машинах неисполнимы: ФИО мы не спрашиваем, квартиры нет.
    # Из интерфейса их убрали, но у части посетителей в кэше лежит старый скрипт,
    # и он всё ещё пришлёт эти флаги — тогда человек заплатит 748 ₽ вместо 449 ₽
    # за услугу, которой не существует. Гасим на сервере: цену определяем мы.
    req.bump = False
    req.kit = False
    if req.object_only:
        # object-only — цена по варианту (A/B/C: 249/349/449). Вариант сохраняем на отчёт,
        # чтобы апселл продавца потом взял дельту по ЭТОМУ варианту (H2-safe, не из живой куки).
        tariff_key = "object"
        amount = _object_price(variant)
        if req.kit:      # пакет к сделке — апселл объектного тарифа
            amount += DEALKIT_PRICE
        amount_override = amount
        rec["variant"] = variant
        _save_report_record(rec)
    elif tariff_key == "base":
        # У недвижимости здесь стояло требование ФИО продавца: без него полный
        # отчёт был бессмыслен. По машине проверяется САМА МАШИНА, ФИО не нужно
        # ни одной базе — а требование осталось бы и рубило каждую оплату
        # полного тарифа четырёхсотой ошибкой, то есть всю основную выручку.
        amount = CS_VARIANTS.get(variant, amount)
        if req.bump:  # order bump: расширение (залоги ФНП), цена в BUMP_PRICE
            amount += BUMP_PRICE
        amount_override = amount

    order = orders.new_order(tariff=tariff_key, amount=amount, report_id=req.report_id)
    orders.update_order(order["id"], variant=order_variant, src=_get_src(request),
                        campaign=request.cookies.get("cs_camp", "") or "прямой/органика",
                        bump=bool(req.bump and tariff_key == "base"),
                        kit=bool(req.kit and tariff_key == "object"),
                        **_order_context(request, req.report_id))
    return_url = f"{_app_base(request)}/pay/return?order={order['id']}"

    try:
        intent = create_payment(tariff_key, order["id"], return_url,
                                email=rec.get("email", ""), amount_override=amount_override)
    except YooKassaError as exc:
        orders.update_order(order["id"], status="failed", mode="yookassa", error=str(exc))
        raise HTTPException(status_code=502, detail=f"ЮKassa: {exc}") from exc

    orders.update_order(
        order["id"],
        payment_id=intent.id,
        status=intent.status,
        mode=intent.mode,
    )

    return JSONResponse(
        {
            "order_id": order["id"],
            "payment_id": intent.id,
            "amount_rub": intent.amount_rub,
            "confirmation_url": intent.confirmation_url,
            "mode": intent.mode,
            "status": intent.status,
            "note": intent.note,
        }
    )


def _return_page(order_id: str, status: str, headline: str, body_html: str) -> HTMLResponse:
    # Счётчик берём из окружения. Здесь стоял номер счётчика НЕДВИЖИМОСТИ,
    # унаследованный вместе с кодом: главная конверсия pay_success — та самая,
    # по которой Директ учится приводить покупателей, — улетала в чужую
    # статистику. Реклама на авто оптимизировалась бы вслепую.
    _cid = (os.getenv("METRIKA_COUNTER_ID") or "").strip()
    _goal = (
        "try{if(!sessionStorage.getItem('ps_'+" + repr(order_id) + ")){"
        "sessionStorage.setItem('ps_'+" + repr(order_id) + ",'1');"
        "ym(" + _cid + ",'reachGoal','pay_success');}}catch(e){}"
        if (status == "succeeded" and _cid.isdigit()) else ""
    )
    # Счётчика нет — не вставляем счётчик вовсе, а не подставляем чужой.
    metrika_block = (
        "<script>"
        + "(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};m[i].l=1*new Date();k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)})(window,document,'script','https://mc.yandex.ru/metrika/tag.js','ym');"
        + "ym(" + _cid + ",'init',{clickmap:true,trackLinks:true,accurateTrackBounce:true});"
        + _goal + "</script>"
    ) if _cid.isdigit() else ""
    palette = {
        "succeeded": "#16a34a",
        "bypass": "#2563EB",
        "pending": "#d97706",
        "waiting_for_capture": "#d97706",
        "canceled": "#dc2626",
        "unknown": "#6b7280",
    }
    color = palette.get(status, "#6b7280")
    html = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ЧистаяСделка — статус оплаты</title>
<style>
  body{{font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#0a0a0a;background:#f8fafc;margin:0;padding:40px 20px}}
  .card{{max-width:560px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:32px}}
  h1{{font-size:22px;letter-spacing:-.02em;margin:0 0 8px}}
  .status{{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#fff;background:{color};border-radius:8px;padding:5px 10px;margin-bottom:16px}}
  p{{color:#374151;font-size:15px;line-height:1.55;margin:8px 0}}
  a.btn{{display:inline-flex;align-items:center;justify-content:center;font-weight:600;background:#2563EB;color:#fff;border-radius:10px;padding:12px 22px;text-decoration:none;margin-top:16px}}
  a.btn:hover{{background:#1d4ed8}}
  a.mini{{color:#6b7280;font-size:14px;margin-top:20px;display:inline-block}}
  .oid{{color:#6b7280;font-size:12.5px;margin-top:20px}}
</style></head>
<body><div class="card">
  <div class="status">{status}</div>
  <h1>{headline}</h1>
  {body_html}
  <div class="oid">Заказ: <code>{order_id}</code></div>
  <a class="mini" href="/">← на главную</a>
</div>{metrika_block}</body></html>"""
    return HTMLResponse(html)


@app.get("/pay/return", response_class=HTMLResponse)
def pay_return(order: str = "") -> HTMLResponse:
    """Возврат пользователя из ЮKassa. НЕ доверяем query-параметрам —
    для реального платежа заново дёргаем GET /v3/payments/{id}."""
    order_id = order.strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="order id required")
    rec = orders.get_order(order_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="order not found")

    report_id = rec.get("report_id") or ""
    pdf_link = (
        f'<a class="btn" href="/api/report/{report_id}" target="_blank" rel="noopener">'
        f"Скачать PDF-отчёт</a>"
        if report_id
        else ""
    )

    if rec.get("mode") == "bypass":
        orders.update_order(order_id, status="bypass")
        if report_id:
            # #4: базовый/объектный отчёт финализируется через _record_paid_order (как в real/webhook),
            # иначе демо-bypass вечно висит в «Формируем отчёт…».
            # Апселлы запускает сам _record_paid_order — под paid_recorded, чтобы
            # повторное открытие страницы не было платным действием.
            _record_paid_order(rec, report_id)
            return RedirectResponse(f"/report/{report_id}/view", status_code=303)
        return _return_page(order_id, "bypass", "Демо-режим оплаты",
                            "<p>Отчёт сформирован.</p>")
    _paid_amt = rec.get("amount", "")

    payment_id = rec.get("payment_id")
    if not payment_id:
        return _return_page(
            order_id,
            "unknown",
            "Платёж не найден",
            "<p>По этому заказу не был инициирован платёж в ЮKassa. "
            "Попробуйте ещё раз с главной страницы.</p>",
        )

    try:
        data = fetch_payment(payment_id)
    except YooKassaError as exc:
        return _return_page(
            order_id,
            "unknown",
            "Не удалось проверить статус оплаты",
            f"<p>ЮKassa вернула ошибку: <code>{exc}</code>.</p>"
            "<p>Это временная неполадка. Если деньги списаны, статус обновится "
            "автоматически по webhook — обновите страницу через 1–2 минуты.</p>",
        )

    status = str(data.get("status") or "unknown")
    orders.update_order(order_id, status=status)

    if status == "succeeded":
        _record_paid_order(rec, report_id)
        # Мониторинг: активация при возврате (webhook в ЛК может быть не настроен —
        # раньше активация была ТОЛЬКО в webhook и подписка зависала оплаченной).
        if rec.get("monitor_sub_id"):
            from modules import monitoring
            monitoring.activate(rec["monitor_sub_id"])
        if report_id:
            # Апселлы уже запущены внутри _record_paid_order (однократно).
            return RedirectResponse(
                f"/report/{report_id}/view?paid={_paid_amt}", status_code=303)
        return _return_page(order_id, "succeeded", "Оплата прошла", "<p>Оплата получена.</p>")
    if status in ("pending", "waiting_for_capture"):
        return _return_page(
            order_id,
            status,
            "Платёж ещё обрабатывается",
            "<p>ЮKassa пока не подтвердила зачисление. Обновите страницу "
            "через 1–2 минуты. Как только платёж пройдёт, страница "
            "покажет ссылку на PDF.</p>",
        )
    if status == "canceled":
        reason = ((data.get("cancellation_details") or {}).get("reason")) or "—"
        return _return_page(
            order_id,
            "canceled",
            "Платёж отклонён",
            f"<p>ЮKassa отклонила оплату (причина: <code>{reason}</code>). "
            "Деньги не списаны. Попробуйте другой способ оплаты с главной страницы.</p>",
        )
    return _return_page(
        order_id,
        "unknown",
        f"Статус платежа: {status}",
        "<p>Необработанный статус. Свяжитесь с поддержкой, указав ID заказа.</p>",
    )


@app.post("/api/yookassa/webhook")
async def yookassa_webhook(request: Request) -> JSONResponse:
    """Webhook ЮKassa (payment.succeeded / payment.canceled / …).
    Тело webhook НЕ является источником истины: мы вытаскиваем из него
    только payment_id и заново читаем платёж через GET /v3/payments/{id}.
    Идемпотентно: повторные события безопасны. Всегда отвечаем 200."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": True, "note": "unparsable body — ignored"})

    obj = payload.get("object") if isinstance(payload, dict) else None
    payment_id = (obj or {}).get("id") if isinstance(obj, dict) else None
    event = payload.get("event") if isinstance(payload, dict) else None

    # Возврат: событие refund.succeeded — объект это refund с полем payment_id.
    # Помечаем заказ возвращённым → is_report_paid вернёт False (доступ к PDF закрыт) +
    # корректный NET в аналитике. Раньше возвраты не обрабатывались вообще.
    if event == "refund.succeeded":
        ref = obj or {}
        pid = ref.get("payment_id")
        o = orders.find_by_payment_id(pid) if pid else None
        if o:
            orders.update_order(o["id"], status="refunded", refunded=True,
                                refund_amount=(ref.get("amount") or {}).get("value"))
        return JSONResponse({"ok": True, "event": event, "refunded": bool(o)})

    if not payment_id:
        return JSONResponse({"ok": True, "note": "no payment id in body"})

    try:
        data = fetch_payment(payment_id)
    except YooKassaError as exc:
        # Не подтверждено API — ничего не помечаем оплаченным.
        return JSONResponse({"ok": True, "note": f"verify failed: {exc}"})

    real_status = str(data.get("status") or "")
    meta = data.get("metadata") or {}
    order_id = meta.get("order_id")

    order = orders.get_order(order_id) if order_id else None
    if order is None:
        order = orders.find_by_payment_id(payment_id)

    if order is None:
        return JSONResponse({"ok": True, "note": "order not found — ignored"})

    if order.get("status") == real_status:
        return JSONResponse({"ok": True, "note": "already applied", "event": event})

    order = orders.update_order(order["id"], status=real_status, payment_id=payment_id) or order
    # Запись в реестр оплат (email из отчёта) + запуск финализации и апселлов.
    # Идемпотентно с pay/return: кто пришёл первым, тот и запустил.
    if real_status == "succeeded":
        _record_paid_order(order, order.get("report_id", ""))
    # Если это заказ мониторинга и он оплачен — активируем подписку.
    if real_status == "succeeded" and order.get("monitor_sub_id"):
        from modules import monitoring
        monitoring.activate(order["monitor_sub_id"])
    return JSONResponse({"ok": True, "event": event, "status": real_status})


# статика (шрифт, будущие ассеты)
# #18: /static/index.html отдался бы сырым (плейсхолдеры цен мимо SSR) — редиректим на «/».
@app.get("/static/index.html", include_in_schema=False)
def _static_index_redirect() -> RedirectResponse:
    return RedirectResponse("/", status_code=301)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
