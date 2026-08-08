"""
Мониторинг продавца до закрытия сделки — свой шедулер.

Пользователь подписывается на слежку за продавцом: периодически (по умолчанию
раз в сутки) перепроверяем ФССП / банкротство / залоги / арбитраж, сравниваем
с предыдущим срезом и при появлении НОВОГО красного флага шлём алерт.

Хранилище — JSON-файлы (как заказы). Шедулер — фоновый поток, запускается
при старте приложения. Алерты — email (SMTP из env) или в лог, если SMTP не
настроен (алерт всё равно сохраняется в подписке и виден в статусе).
"""
from __future__ import annotations

import json
import os
import smtplib
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.getenv("MONITOR_DIR", "data/monitor"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------- хранилище подписок ----------

def _path(sub_id: str) -> Path:
    return DATA_DIR / f"{sub_id}.json"


def save_sub(sub: dict[str, Any]) -> None:
    _path(sub["id"]).write_text(
        json.dumps(sub, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def load_sub(sub_id: str) -> dict[str, Any] | None:
    p = _path(sub_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def all_subs() -> list[dict[str, Any]]:
    subs: list[dict[str, Any]] = []
    for p in DATA_DIR.glob("*.json"):
        try:
            subs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return subs


def subs_for_email(email: str) -> list[dict[str, Any]]:
    return [s for s in all_subs() if s.get("email") == email]


# ---------- сигнатура среза (для diff) ----------

def _signature(checks: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Множество «ключей» найденного по каждой проверке — для сравнения.

    Новый ключ, которого не было в прошлом срезе, = изменение (красный флаг).
    """
    sig: dict[str, list[str]] = {}
    for c in checks:
        key = c.get("key", "")
        items = c.get("items") or []
        ids: list[str] = []
        for it in items:
            if isinstance(it, dict):
                ids.append(
                    str(it.get("case") or it.get("number") or it.get("subject")
                        or it.get("stage") or json.dumps(it, ensure_ascii=False, sort_keys=True))
                )
            else:
                ids.append(str(it))
        # статус found без items тоже фиксируем (напр. банкротство found)
        if c.get("status") == "found" and not ids:
            ids.append("__found__")
        sig[key] = sorted(set(ids))
    return sig


def _diff(old: dict[str, list[str]], new: dict[str, list[str]]) -> list[str]:
    """Новые красные флаги: ключи, появившиеся в new и отсутствовавшие в old."""
    changes: list[str] = []
    names = {
        "bankruptcy": "Банкротство продавца",
        "enforcement": "Исполнительные производства (ФССП)",
        "arbitration": "Арбитражные дела",
        "pledges": "Залоги и обременения",
        "passport": "Паспорт",
    }
    for key, new_ids in new.items():
        old_ids = set(old.get(key, []))
        added = [i for i in new_ids if i not in old_ids]
        if added:
            changes.append(f"{names.get(key, key)}: появилось новых записей — {len(added)}")
    return changes


# ---------- создание подписки ----------

def create_subscription(
    email: str, seller_name: str, seller_dob: str, object_ref: str,
    seller_inn: str = "", days: int = 30, interval_hours: int = 24,
    baseline_checks: list[dict[str, Any]] | None = None,
    status: str = "pending_payment", report_id: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    sub = {
        "id": uuid.uuid4().hex[:16],
        "report_id": report_id,
        "email": email,
        "seller_name": seller_name,
        "seller_dob": seller_dob,
        "object_ref": object_ref,
        "seller_inn": seller_inn,
        "created_at": now.isoformat(timespec="seconds"),
        "days": days,
        "expires_at": (now + timedelta(days=days)).isoformat(timespec="seconds"),
        "interval_hours": interval_hours,
        "last_run": now.isoformat(timespec="seconds"),
        "next_run": (now + timedelta(hours=interval_hours)).isoformat(timespec="seconds"),
        "status": status,  # pending_payment | active | cancelled | expired
        "baseline": _signature(baseline_checks or []),
        "runs": 0,
        "alerts": [],
    }
    save_sub(sub)
    return sub


def sub_for_report(report_id: str) -> dict[str, Any] | None:
    """Последняя подписка мониторинга, созданная с этого отчёта (для UI отчёта)."""
    if not report_id:
        return None
    best = None
    for s in all_subs():
        if s.get("report_id") == report_id:
            if best is None or s.get("created_at", "") > best.get("created_at", ""):
                best = s
    return best


def activate(sub_id: str) -> dict[str, Any] | None:
    """Активировать подписку после оплаты — отсчёт срока и интервала с этого
    момента. ИДЕМПОТЕНТНО: повторный вызов (повторный заход на /pay/return по
    сохранённой ссылке) НЕ продлевает срок и НЕ реанимирует отменённую подписку."""
    sub = load_sub(sub_id)
    if not sub:
        return None
    # Активируем ТОЛЬКО из состояния ожидания оплаты. Уже активная/отменённая/
    # истёкшая — не трогаем (иначе бесплатное бесконечное продление).
    if sub.get("status") != "pending_payment" or sub.get("activated_at"):
        return sub
    now = datetime.now(timezone.utc)
    sub["status"] = "active"
    sub["activated_at"] = now.isoformat(timespec="seconds")
    sub["expires_at"] = (now + timedelta(days=sub.get("days", 30))).isoformat(timespec="seconds")
    sub["last_run"] = now.isoformat(timespec="seconds")
    sub["next_run"] = (now + timedelta(hours=sub.get("interval_hours", 24))).isoformat(timespec="seconds")
    save_sub(sub)
    return sub


def cancel_subscription(sub_id: str) -> bool:
    sub = load_sub(sub_id)
    if not sub:
        return False
    sub["status"] = "cancelled"
    save_sub(sub)
    return True


# ---------- алерт ----------

def tg_api(method: str, payload: dict, timeout: float = 15) -> dict | None:
    """Вызов Telegram Bot API через прокси (сервер в РФ не достаёт
    api.telegram.org напрямую), с фолбэком на прямой доступ при сбое сети."""
    import httpx
    tok = os.getenv("BOT_TOKEN", "").strip()
    if not tok:
        return None
    url = f"https://api.telegram.org/bot{tok}/{method}"
    proxy = os.getenv("LLM_PROXY", "").strip() or None
    for px in ([proxy, None] if proxy else [None]):
        try:
            with httpx.Client(proxy=px, timeout=timeout) as client:
                r = client.post(url, json=payload)
            return r.json()
        except Exception:
            continue
    return None


def _process_update(update: dict) -> None:
    """/start <sub_id> → привязать chat_id к подписке мониторинга."""
    msg = update.get("message") or update.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    if not chat_id:
        return
    # Админские команды (/admin, /stat, /check) — в отдельном модуле, чтобы
    # клиентский поллинг мониторинга не смешивался с внутренней статистикой.
    try:
        from modules import adminbot
        if adminbot.handle_command(text, chat_id):
            return
    except Exception as e:  # noqa: BLE001 — админка не имеет права ломать клиентский бот
        print(f"[admin] команда не обработана: {type(e).__name__}: {e}", flush=True)
    if not text.startswith("/start"):
        return
    parts = text.split(maxsplit=1)
    sub_id = parts[1].strip() if len(parts) > 1 else ""
    if sub_id and link_telegram(sub_id, chat_id):
        send_telegram(
            chat_id,
            "✓ Telegram подключён к мониторингу продавца.\n"
            "Пришлём сюда сообщение, если в госреестрах появится красный флаг "
            "(банкротство, долги, аресты, суды).",
        )
    else:
        send_telegram(
            chat_id,
            "Здравствуйте! Это бот уведомлений сервиса ЧистаяСделка. "
            "Чтобы подключить мониторинг, оформите его на https://chistasdelka.ru",
        )


def _poll_loop() -> None:
    """Long-polling getUpdates через прокси: Telegram НЕ доставляет webhook на
    РФ-сервер (Connection timed out), поэтому опрашиваем сами исходящими.

    Цикл ОБЯЗАН говорить в лог, когда связь пропала. 30.07.2026 провайдер
    прокси сменил IP, канал до Telegram умер целиком — и бот молчал сорок
    минут, потому что здесь все исключения глотались без единой строчки.
    Диагноз занял полчаса вместо одной команды `docker logs`."""
    import time
    offset = 0
    fails = 0
    tg_api("deleteWebhook", {"drop_pending_updates": False})
    while True:
        try:
            data = tg_api("getUpdates",
                          {"offset": offset, "timeout": 25, "allowed_updates": ["message"]},
                          timeout=35)
            if not data or not data.get("ok"):
                fails += 1
                # Кричим на первом сбое и потом раз в ~5 минут, чтобы лог не залило.
                if fails == 1 or fails % 60 == 0:
                    why = (data or {}).get("description") or "нет ответа (сеть/прокси)"
                    print(f"[tg] getUpdates не работает {fails}-й раз подряд: {why}",
                          flush=True)
                time.sleep(5)
                continue
            if fails:
                print(f"[tg] связь с Telegram восстановлена после {fails} сбоев", flush=True)
                fails = 0
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                try:
                    _process_update(upd)
                except Exception as e:  # noqa: BLE001 — одно битое сообщение не роняет цикл
                    print(f"[tg] апдейт не обработан: {type(e).__name__}: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            fails += 1
            if fails == 1 or fails % 60 == 0:
                print(f"[tg] цикл опроса упал ({fails}-й раз): {type(e).__name__}: {e}",
                      flush=True)
            time.sleep(5)


def start_polling() -> None:
    import threading
    if os.getenv("BOT_TOKEN", "").strip():
        threading.Thread(target=_poll_loop, daemon=True).start()


def send_telegram(chat_id: str | int, text: str) -> bool:
    """Отправить сообщение через Telegram Bot API. Возвращает успех."""
    if not chat_id:
        return False
    data = tg_api("sendMessage",
                  {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
    ok = bool(data and data.get("ok"))
    if not ok:
        # Через этот канал уходят тревоги «оплачено, а услуга не оказана».
        # Молча потерянная тревога — это потерянные деньги, поэтому в лог.
        why = (data or {}).get("description") or "нет ответа (сеть/прокси)"
        print(f"[tg] сообщение в {chat_id} НЕ доставлено: {why}", flush=True)
    return ok


def _send_alert(sub: dict[str, Any], changes: list[str]) -> None:
    text = "\n".join([
        f"⚠️ ЧистаяСделка — изменения по продавцу",
        f"{sub['seller_name']} (объект {sub['object_ref']})",
        "",
        "В госреестрах появились НОВЫЕ сведения:",
        *[f"• {c}" for c in changes],
        "",
        "Рекомендуем приостановить сделку и перепроверить продавца до подписания.",
        "Детали — на https://chistasdelka.ru",
    ])
    alert_rec = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "changes": changes,
        "delivered": False,
        "channel": None,
    }
    # 1) Telegram (если пользователь подключил бота)
    if sub.get("tg_chat_id") and send_telegram(sub["tg_chat_id"], text):
        alert_rec["delivered"] = True
        alert_rec["channel"] = "telegram"
    # 2) Email как запасной канал (если настроен SMTP)
    elif os.getenv("SMTP_HOST", "").strip():
        try:
            msg = MIMEText(text, "plain", "utf-8")
            msg["Subject"] = f"⚠️ ЧистаяСделка: изменения по продавцу {sub['seller_name']}"
            msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "noreply@chistasdelka.ru"))
            msg["To"] = sub["email"]
            port = int(os.getenv("SMTP_PORT", "465"))
            user, pw = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "")
            if port == 465:
                with smtplib.SMTP_SSL(os.getenv("SMTP_HOST"), port, timeout=20) as s:
                    if user:
                        s.login(user, pw)
                    s.sendmail(msg["From"], [sub["email"]], msg.as_string())
            else:
                with smtplib.SMTP(os.getenv("SMTP_HOST"), port, timeout=20) as s:
                    s.starttls()
                    if user:
                        s.login(user, pw)
                    s.sendmail(msg["From"], [sub["email"]], msg.as_string())
            alert_rec["delivered"] = True
            alert_rec["channel"] = "email"
        except Exception as e:  # noqa: BLE001
            alert_rec["error"] = f"{type(e).__name__}: {e}"
    sub.setdefault("alerts", []).append(alert_rec)


def link_telegram(sub_id: str, chat_id: str | int) -> dict[str, Any] | None:
    """Привязать Telegram chat_id к подписке (по deep-link /start <sub_id>)."""
    sub = load_sub(sub_id)
    if not sub:
        return None
    sub["tg_chat_id"] = chat_id
    save_sub(sub)
    return sub


# ---------- один прогон подписки ----------

def _run_checks_fn():
    # ленивый импорт, чтобы избежать циклической зависимости с app
    from app import _run_checks  # type: ignore
    return _run_checks


def _regioncode(sub: dict[str, Any]) -> str:
    """Код региона для ФССП. Кэшируем в подписке: без объекта его больше неоткуда взять.

    Источники по убыванию надёжности: уже сохранённый код → кадастр из object_ref →
    кадастр из превью связанного отчёта (там он лежит после бесплатного НСПД)."""
    from modules.fssp import region_from_kadastr

    code = str(sub.get("regioncode") or "")
    if not code:
        code = region_from_kadastr(sub.get("object_ref", ""))
    if not code and sub.get("report_id"):
        try:
            from app import _load_report  # type: ignore
            rec = _load_report(sub["report_id"]) or {}
            code = region_from_kadastr((rec.get("object_preview") or {}).get("cad") or "")
        except Exception:  # noqa: BLE001 — отсутствие отчёта не должно валить прогон
            pass
    if code:
        sub["regioncode"] = code
    return code


# Порог баланса NewDB, ниже которого прогон не запускаем. Тот же, что в /api/pay:
# меньше — не хватит на полную проверку продавца, и мы заплатим за огрызок.
_MIN_BALANCE = 30
# Сколько прогонов подряд может провалиться, прежде чем подписка встаёт на паузу.
_MAX_FAILS = 3


def run_subscription(sub: dict[str, Any]) -> None:
    """Перепроверить продавца, сравнить с baseline, при изменениях — алерт.

    ПОРЯДОК ЗДЕСЬ — ЗАЩИТА ОТ ПОВТОРА АВАРИИ 25.07.2026. Раньше next_run
    сдвигался в самом конце, после платных проверок и отправки алерта. Любое
    исключение между оплатой и save_sub оставляло на диске вчерашний next_run,
    шедулер считал, что время снова пришло, и через 5 минут платил заново —
    до 2592 платных запросов в сутки на одну подписку за 990 руб.

    Теперь «занимаем» слот ДО первого платного вызова: сдвигаем next_run и
    сохраняем. Упадём — повтор будет через сутки, а не через пять минут.
    """
    from app import CheckRequest  # type: ignore

    now = datetime.now(timezone.utc)

    # Шаг 0. Резервируем следующий запуск ДО любых трат. Сохраняем сразу на диск:
    # с этого момента крах в любой точке ниже не приводит к повторному списанию.
    sub["last_run"] = now.isoformat(timespec="seconds")
    sub["next_run"] = (now + timedelta(hours=sub.get("interval_hours", 24))).isoformat(timespec="seconds")
    save_sub(sub)

    # Шаг 1. Гейт баланса. Шедулер тратит без человека, поэтому проверяем ДО
    # запроса: не знаем баланс (провайдер лёг) — тоже не идём, деньги дороже суток
    # задержки. Отсутствие данных = отказ, а не «наверное, всё хорошо».
    try:
        from modules import newdb
        bal = newdb.balance()
    except Exception:  # noqa: BLE001
        bal = None
    if bal is None or bal < _MIN_BALANCE:
        sub["skipped_runs"] = sub.get("skipped_runs", 0) + 1
        sub["last_skip"] = f"баланс NewDB: {'неизвестен' if bal is None else bal}"
        save_sub(sub)
        print(f"[monitor] пропуск прогона sub={sub.get('id')}: {sub['last_skip']}", flush=True)
        return

    req = CheckRequest(
        seller_name=sub["seller_name"], seller_dob=sub.get("seller_dob", ""),
        object_ref=sub.get("object_ref", ""), seller_inn=sub.get("seller_inn", ""),
        tariff="ext", consent=True,
    )
    # Шаг 2. Платные проверки. skip_object=True: объект в ЕГРН между сутками не
    # меняется, а мониторинг следит за ПРОДАВЦОМ — платить за ЕГРН ежедневно незачем.
    # Но без объекта неоткуда взять регион для ФССП, а без региона ФССП молча
    # отдаёт "не проверено". Поэтому регион разрешаем один раз и запоминаем.
    try:
        checks = _run_checks_fn()(
            req, skip_object=True, regioncode=_regioncode(sub),
        )
    except Exception as e:  # noqa: BLE001
        # Счётчик неудач: три подряд — подписка на паузу и разбор руками. Иначе
        # сломанный прогон тихо жёг бы деньги каждые сутки до конца срока.
        fails = sub.get("fails", 0) + 1
        sub["fails"] = fails
        sub["last_error"] = f"{type(e).__name__}: {e}"[:200]
        if fails >= _MAX_FAILS:
            sub["status"] = "paused"
            print(f"[ALERT] monitor: подписка {sub.get('id')} на паузе — "
                  f"{fails} неудачных прогона подряд: {sub['last_error']}", flush=True)
        save_sub(sub)
        return
    sub["fails"] = 0  # успешный прогон обнуляет счётчик

    serialized = []
    for c in checks:
        serialized.append({"key": c.key, "status": c.status, "items": c.items})
    new_sig = _signature(serialized)
    changes = _diff(sub.get("baseline", {}), new_sig)
    sub["runs"] = sub.get("runs", 0) + 1
    sub["baseline"] = new_sig  # обновляем срез, чтобы алерт был один раз на изменение
    if changes:
        _send_alert(sub, changes)
    else:
        # Ежедневный «пульс»: тишина пугает — подтверждаем, что проверка прошла
        # и изменений нет. Только Telegram (email не спамим), тихо глотаем сбои.
        if sub.get("tg_chat_id"):
            try:
                left = ""
                try:
                    days_left = (datetime.fromisoformat(sub["expires_at"]) - now).days
                    left = f" Мониторинг активен ещё {max(days_left, 0)} дн."
                except Exception:
                    pass
                send_telegram(
                    sub["tg_chat_id"],
                    f"✅ ЧистаяСделка: плановая перепроверка продавца "
                    f"{sub.get('seller_name','')} выполнена — изменений нет. "
                    f"Банкротство, долги ФССП, залоги: без новых записей.{left}",
                )
            except Exception:
                pass
    save_sub(sub)


# ---------- фоновый шедулер ----------

_scheduler_started = False


def _scheduler_loop() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            for sub in all_subs():
                if sub.get("status") != "active":
                    continue
                try:
                    if now >= datetime.fromisoformat(sub["expires_at"]):
                        sub["status"] = "expired"
                        save_sub(sub)
                        continue
                    if now >= datetime.fromisoformat(sub["next_run"]):
                        run_subscription(sub)
                except Exception as e:  # noqa: BLE001
                    # Молчаливый continue прятал ровно тот сбой, который жёг деньги.
                    # Сам run_subscription уже сдвинул next_run, повтора через 5 мин не будет.
                    print(f"[monitor] прогон sub={sub.get('id')} упал: "
                          f"{type(e).__name__}: {e}", flush=True)
                    continue
        except Exception as e:  # noqa: BLE001
            print(f"[monitor] обход подписок упал: {type(e).__name__}: {e}", flush=True)
        time.sleep(300)  # тик раз в 5 минут


def start_scheduler() -> None:
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()
