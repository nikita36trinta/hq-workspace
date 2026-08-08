"""Админ-канал в Telegram: статистика по команде и сторож недоставленных оплат.

Два независимых куска:
  1. Команды в боте (@ChistaSdelkaAlertBot): /stat — продажи за сегодня и всего.
  2. Сторож: раз в 10 минут ищет ОПЛАЧЕННЫЕ заказы без результата и пишет о них.

Почему сторож вообще нужен: за сутки дважды случилось «деньги взяли, услугу не
оказали» — апселл продавца терял регион для ФССП (26.07), апселл банкротства не
запускался после того, как из формы убрали ИНН (27.07). Оба раза узнавали по
выписке из кассы, а не от системы. Тишина сторожа не означает, что всё хорошо —
он ловит ровно три сценария, перечисленные в checks().
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("SDELKA_DATA_DIR", "data"))
ADMIN_FILE = DATA_DIR / "admin_chat.json"
SEEN_FILE = DATA_DIR / "admin_seen.json"

# Код для привязки: /admin <код>. Задаётся в окружении, иначе привязка выключена
# (без кода любой, кто найдёт бота, получил бы доступ к выручке).
ADMIN_CODE = os.getenv("ADMIN_TG_CODE", "").strip()

SWEEP_PERIOD_SEC = 600          # раз в 10 минут
UNDELIVERED_AFTER_MIN = 15      # проверки идут до 5 минут; 15 — с запасом

# Сколько автоповторов финализации делает app._retry_stuck_once. Константа живёт
# ЗДЕСЬ, а app импортирует её отсюда: сторож и повторщик обязаны считать попытки
# одинаково, иначе тревога снова начнёт обгонять починку.
RETRY_MAX = 3
# Предел молчания. Автоповтор идёт не чаще раза в 15 минут и только когда
# источник жив — если источник лёг, попытки не тратятся и счётчик стоит на месте.
# Без этого предела заказ в таком состоянии не поднял бы тревогу НИКОГДА, а это
# ровно тот случай, ради которого сторож и написан.
ALERT_LATEST_MIN = 60
LOW_BALANCE_RUB = int(os.getenv("NEWDB_MIN_BALANCE", "50") or 50)  # см. app._alert_low_balance


# ── хранение chat_id админа ───────────────────────────────────────────────
def admin_chats() -> list[int]:
    """Владелец (OWNER_TG_CHAT_ID) — админ всегда: его чат уже известен, ради
    него не нужно проходить привязку по коду. Остальные добавляются /admin <код>."""
    out: list[int] = []
    owner = os.getenv("OWNER_TG_CHAT_ID", "").strip()
    if owner.lstrip("-").isdigit():
        out.append(int(owner))
    try:
        out += [int(x) for x in json.loads(ADMIN_FILE.read_text(encoding="utf-8"))]
    except Exception:  # noqa: BLE001 — нет файла/битый файл: только владелец
        pass
    return list(dict.fromkeys(out))


def _add_admin(chat_id: int) -> bool:
    chats = admin_chats()
    if chat_id in chats:
        return False
    chats.append(chat_id)
    ADMIN_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_FILE.write_text(json.dumps(chats), encoding="utf-8")
    return True


def notify(text: str) -> int:
    """Отправить всем админам. Возвращает число доставленных."""
    from modules import monitoring
    return sum(1 for c in admin_chats() if monitoring.send_telegram(c, text))


# ── данные ────────────────────────────────────────────────────────────────
def _orders() -> list[dict[str, Any]]:
    out = []
    for f in (DATA_DIR / "orders").glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return out


def _report(rid: str) -> dict[str, Any] | None:
    try:
        return json.loads((DATA_DIR / "reports" / f"{rid}.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _fmt_money(v: int) -> str:
    return f"{v:,}".replace(",", " ")


# Понятные названия вместо служебных ключей тарифа: в отчёте видно, ЗА ЧТО платили.
TARIFF_RU = {
    "base": "Полный отчёт — объект и продавец",
    "object": "Проверка объекта",
    "ext": "Расширенная проверка",
    "addon": "Доплата: банкротство продавца",
    "addon_seller": "Доплата: проверка продавца",
    "monitor": "Наблюдение за продавцом",
    "realtor": "Пакет риелтору",
}

MSK = timezone(timedelta(hours=3))   # считаем сутки по Москве, а не по UTC


def _day(o: dict[str, Any]) -> str:
    """Дата оплаты по московскому времени (YYYY-MM-DD)."""
    try:
        t = datetime.fromisoformat(str(o.get("created_at")).replace("Z", "+00:00"))
        return t.astimezone(MSK).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return ""


def _money(rows: list[dict[str, Any]]) -> int:
    return sum(int(float(r.get("amount") or 0)) for r in rows)


def _delta(now: int, was: int) -> str:
    """«+37%» / «−12%» / «—», словами понятными без пояснений."""
    if was == 0:
        return "прошлый период пустой" if now else "—"
    pct = round((now - was) / was * 100)
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct}%"


def _breakdown(rows: list[dict[str, Any]], indent: str = "   ") -> list[str]:
    by: dict[str, list] = {}
    for o in rows:
        by.setdefault(str(o.get("tariff") or "?"), []).append(o)
    out = []
    for t, rr in sorted(by.items(), key=lambda x: -_money(x[1])):
        name = TARIFF_RU.get(t, t)
        out.append(f"{indent}{name}: {len(rr)} шт — {_fmt_money(_money(rr))} ₽")
    return out


def stats_text() -> str:
    """Продажи: сегодня, вчера, 3 дня, неделя + сравнение периодов."""
    now = datetime.now(MSK)
    d = lambda n: (now - timedelta(days=n)).strftime("%Y-%m-%d")  # noqa: E731
    ok = [o for o in _orders() if o.get("status") == "succeeded"]
    for o in ok:
        o["_d"] = _day(o)

    def between(a: int, b: int) -> list[dict[str, Any]]:
        """Оплаты за период [сегодня-a … сегодня-b], границы включительно."""
        lo, hi = d(a), d(b)
        return [o for o in ok if lo <= o["_d"] <= hi]

    today, yest = between(0, 0), between(1, 1)
    d3, week, prev_week = between(2, 0), between(6, 0), between(13, 7)

    L = [f"📊 ЧистаяСделка · {now.strftime('%d.%m.%Y %H:%M')} МСК", ""]

    L.append(f"СЕГОДНЯ: {len(today)} оплат — {_fmt_money(_money(today))} ₽")
    L += _breakdown(today) or ["   пока пусто"]
    L.append("")
    L.append(f"ВЧЕРА: {len(yest)} оплат — {_fmt_money(_money(yest))} ₽")
    L += _breakdown(yest) or ["   было пусто"]
    L.append("")
    L.append(f"Сегодня к вчера: по деньгам {_delta(_money(today), _money(yest))}, "
             f"по числу оплат {_delta(len(today), len(yest))}")
    L.append("")
    L.append(f"ЗА 3 ДНЯ: {len(d3)} оплат — {_fmt_money(_money(d3))} ₽")
    L.append(f"ЗА НЕДЕЛЮ (7 дней): {len(week)} оплат — {_fmt_money(_money(week))} ₽")
    L += _breakdown(week)
    L.append("")
    L.append(f"ПРЕДЫДУЩАЯ НЕДЕЛЯ (8–14 дней назад): {len(prev_week)} оплат — "
             f"{_fmt_money(_money(prev_week))} ₽")
    L.append(f"Неделя к неделе: по деньгам {_delta(_money(week), _money(prev_week))}, "
             f"по числу оплат {_delta(len(week), len(prev_week))}")
    if week:
        L.append(f"Средний чек за неделю: {_money(week) // len(week)} ₽")
    L.append("")
    L.append(f"ВСЕГО ЗА ВСЁ ВРЕМЯ: {len(ok)} оплат — {_fmt_money(_money(ok))} ₽")
    L += _breakdown(ok)

    # что мешает продавать прямо сейчас
    orders = _orders()
    pend = [o for o in orders if o.get("status") in ("pending", "failed")
            and _day(o) == d(0)]
    ours, waiting = undelivered_split()
    L += ["", "ТРЕВОГИ",
          f"   Начали платить, но не оплатили (сегодня): {len(pend)}",
          f"   Оплачено, а услуга не оказана: {len(ours)}" + (" ⚠️" if ours else " — нет")]
    if waiting:
        L.append(f"   Запросили данные у клиента, ждём ответа: {len(waiting)} "
                 f"({_fmt_money(sum(b['amount'] for b in waiting))} ₽)")
    try:
        from modules import newdb
        bal = newdb.balance()
        L.append(f"   Баланс у поставщика данных: "
                 f"{bal if bal is not None else 'не отвечает'} ₽"
                 + (" ⚠️ пополнить" if (bal is None or bal < LOW_BALANCE_RUB) else ""))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(L)


def undelivered() -> list[dict[str, Any]]:
    """Оплаченные заказы, по которым через 15 минут нет результата."""
    now = datetime.now(timezone.utc)
    edge = now - timedelta(minutes=UNDELIVERED_AFTER_MIN)
    out = []
    for o in _orders():
        if o.get("status") != "succeeded" or not o.get("report_id"):
            continue
        try:
            paid_at = datetime.fromisoformat(str(o.get("created_at")).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            continue
        if paid_at > edge:
            continue                       # ещё рано, проверки идут
        rec = _report(o["report_id"])
        if not rec:
            continue
        tariff = str(o.get("tariff") or "")
        why = ""
        if tariff in ("base", "object", "ext"):
            done = [c for c in rec.get("checks") or []
                    if c.get("status") in ("found", "not_found", "not_checked")]
            if rec.get("preview") or not done:
                why = "отчёт не финализирован"
        elif tariff == "addon":
            if not rec.get("addon_applied") and not ((rec.get("addon_result") or {}).get("checks")):
                why = "проверка банкротства не выполнена"
        elif tariff == "addon_seller":
            if not rec.get("seller_added"):
                why = "проверка продавца не выполнена"
        if why:
            # Ход не всегда за нами. Если мы уже запросили у человека кадастровый
            # номер или адрес, заявка ждёт ЕГО ответа, и в тревоге «разбирать нам»
            # ей не место: иначе семь отработанных писем читаются как семь дел,
            # к которым не притрагивались, и настоящее новое тонет среди них.
            stage = "waiting" if rec.get("ask_sent") else "ours"
            # Ход может быть и за автоповтором. Сторож просыпается через 15 минут
            # после оплаты — ровно тогда же уходит первая попытка, и тревога
            # обгоняла починку: 07.08 заказ e4e57d2815fb доехал сам, а письмо
            # «услуга не оказана» уже лежало в чате. Ждём, пока попытки кончатся.
            attempts = int(rec.get("finalize_attempts") or 0)
            if stage == "ours" and not rec.get("finalize_permanent") \
                    and attempts < RETRY_MAX and paid_at > now - timedelta(minutes=ALERT_LATEST_MIN):
                stage = "retrying"
            if rec.get("deferred"):
                stage = "deferred"     # решение принято, напоминать не о чем
            out.append({"order": o["id"], "report": o["report_id"], "tariff": tariff,
                        "amount": int(float(o.get("amount") or 0)), "why": why,
                        "paid_at": str(o.get("created_at"))[:19], "stage": stage,
                        "attempts": attempts,
                        "email": rec.get("email") or o.get("email") or ""})
    return out


def undelivered_split() -> tuple[list, list]:
    """(наши, ждём клиента) — отложенные не возвращаем вовсе."""
    rows = [b for b in undelivered() if b.get("stage") != "deferred"]
    return ([b for b in rows if b.get("stage") != "waiting"],
            [b for b in rows if b.get("stage") == "waiting"])


# ── сторож ────────────────────────────────────────────────────────────────
def _seen() -> set[str]:
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def _remember(keys: set[str]) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(keys)), encoding="utf-8")


def sweep_once() -> int:
    """Один проход сторожа. Возвращает число отправленных тревог.
    О каждом заказе сообщаем ОДИН раз — иначе через час это станет шумом,
    который перестают читать."""
    if not admin_chats():
        return 0
    sent = 0
    seen = _seen()
    # Тревожим только по тому, что на НАШЕЙ стороне: отложенные и те, у кого мы
    # уже спросили недостающие данные, ждут не нас.
    bad = [b for b in undelivered() if b.get("stage") == "ours"]
    fresh = [b for b in bad if b["order"] not in seen]
    if fresh:
        lines = ["🚨 Оплачено, но услуга не оказана", ""]
        for b in fresh:
            # Тревога уходит только после автоповторов, поэтому сразу говорим,
            # чем они кончились: «0 попыток» — источник лежал и повтор не пошёл,
            # «3 из 3» — чинили и не вышло, руками тоже, скорее всего, не выйдет.
            tried = (f" · автоповтор {b['attempts']}/{RETRY_MAX}"
                     if b.get("attempts") else " · автоповтор не пошёл")
            lines.append(f"• {b['amount']} ₽ · {b['tariff']} · {b['why']}{tried}")
            lines.append(f"  отчёт {b['report']} · оплата {b['paid_at']}")
            lines.append(f"  https://chistasdelka.ru/report/{b['report']}/view")
        notify("\n".join(lines))
        sent += 1
        seen |= {b["order"] for b in fresh}

    _remember(seen)
    return sent


def sweep_loop() -> None:
    while True:
        try:
            sweep_once()
        except Exception as e:  # noqa: BLE001 — сторож не имеет права ронять приложение
            print(f"[admin] сторож упал: {type(e).__name__}: {e}", flush=True)
        time.sleep(SWEEP_PERIOD_SEC)


def start_sweeper() -> None:
    import threading
    threading.Thread(target=sweep_loop, daemon=True).start()


# ── команды бота ──────────────────────────────────────────────────────────
def handle_command(text: str, chat_id: int) -> bool:
    """Обработать админ-команду. True — команда распознана и отвечена."""
    from modules import monitoring
    t = (text or "").strip()
    low = t.lower()

    if low.startswith("/admin"):
        code = t.split(maxsplit=1)[1].strip() if len(t.split(maxsplit=1)) > 1 else ""
        if not ADMIN_CODE:
            monitoring.send_telegram(chat_id, "Привязка выключена: не задан ADMIN_TG_CODE.")
        elif code == ADMIN_CODE:
            added = _add_admin(chat_id)
            monitoring.send_telegram(
                chat_id,
                ("✓ Этот чат подключён к админ-уведомлениям.\n\n"
                 if added else "Этот чат уже подключён.\n\n") +
                "Команды:\n/stat — продажи за сегодня и всего\n"
                "/check — что сейчас оплачено без результата")
        else:
            monitoring.send_telegram(chat_id, "Неверный код.")
        return True

    if low.startswith(("/stat", "/стат")):
        if chat_id not in admin_chats():
            monitoring.send_telegram(chat_id, "Команда доступна только администратору.")
            return True
        monitoring.send_telegram(chat_id, stats_text())
        return True

    if low.startswith("/check"):
        if chat_id not in admin_chats():
            return True
        bad = undelivered()
        if not bad:
            monitoring.send_telegram(chat_id, "✓ Оплаченных заказов без результата нет.")
        else:
            lines = ["🚨 Оплачено без результата:", ""]
            for b in bad:
                lines.append(f"• {b['amount']} ₽ · {b['tariff']} · {b['why']}")
                lines.append(f"  https://chistasdelka.ru/report/{b['report']}/view")
            monitoring.send_telegram(chat_id, "\n".join(lines))
        return True

    return False
