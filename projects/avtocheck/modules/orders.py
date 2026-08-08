"""
Хранилище заказов: in-memory dict + snapshot на диск в `data/orders/{id}.json`.
Тот же паттерн, что для отчётов в app.py — переживает рестарт uvicorn.

Заказ связывает оплату и отчёт:
  id, tariff, amount, report_id, payment_id, status, mode,
  created_at, updated_at.

status ∈ {"created", "pending", "waiting_for_capture", "succeeded",
          "canceled", "bypass"}.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


ORDERS_DIR = Path(__file__).resolve().parent.parent / "data" / "orders"
ORDERS_DIR.mkdir(parents=True, exist_ok=True)

# Единый реестр оплат (append-only) — база «кто/сколько/когда оплатил».
PAYMENTS_LOG = ORDERS_DIR.parent / "payments.jsonl"

ORDERS: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def record_payment(order: dict[str, Any], email: str) -> None:
    """Идемпотентно записать успешную оплату в реестр (data/payments.jsonl).
    Повторные вызовы (pay/return + webhook) не дублируют — по флагу на заказе."""
    if order.get("paid_recorded"):
        return
    rec = {
        "paid_at": _now(),
        "email": (email or "").strip(),
        "amount": order.get("amount"),
        "tariff": order.get("tariff"),
        "variant": order.get("variant", ""),
        "src": order.get("src", ""),        # источник трафика: ad | organic
        "campaign": order.get("campaign", ""),  # кампания (utm_campaign/yclid) для ROAS
        "bump": bool(order.get("bump")),    # взяли расширенный к полному тарифу
        "kit": bool(order.get("kit")),      # взяли пакет к сделке к объектному тарифу
        "order_id": order.get("id"),
        "payment_id": order.get("payment_id") or "",
        "status": order.get("status"),
        # Раньше реестр знал только про деньги: сумму, тариф, вариант, кампанию.
        # Поэтому «участки платят хуже квартир?» и «мобильные платят меньше?»
        # деньгами не проверялись вообще — только косвенно, по событиям, где нет сумм.
        "report_id": order.get("report_id") or "",
        "device": order.get("device") or "",      # смартфон / планшет / десктоп
        "obj_kind": order.get("obj_kind") or "",  # кадастр / адрес
        "region": order.get("region") or "",      # регион ОБЪЕКТА, не покупателя
        # Оплата из режима «не считать меня» (/?notrack=1). Запись остаётся в реестре —
        # деньги-то реальные и чек выбит, — но в выручку, CVR и A/B не идёт.
        "test": bool(order.get("test")),
    }
    try:
        with open(PAYMENTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        order["paid_recorded"] = True
        _persist(order)
    except Exception:
        pass


def read_payments() -> list[dict[str, Any]]:
    """Прочитать весь реестр оплат (для админ-экспорта/аналитики).

    #14: реестр payments.jsonl append-only, а возврат (refund.succeeded) метит только
    order-файл (refunded/chargeback/status='refunded'). Обогащаем каждую запись реестра
    актуальным статусом возврата из соответствующего заказа — иначе возвращённые платежи
    навсегда остаются в CVR/выручке."""
    out: list[dict[str, Any]] = []
    if not PAYMENTS_LOG.exists():
        return out
    # индекс возвратов по order_id (из order-файлов)
    refunded_ids: dict[str, dict[str, Any]] = {}
    try:
        for path in ORDERS_DIR.glob("*.json"):
            try:
                o = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if o.get("refunded") or o.get("chargeback") or o.get("status") == "refunded":
                refunded_ids[o.get("id", "")] = o
    except Exception:
        pass
    for line in PAYMENTS_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        oid = rec.get("order_id") or ""
        if oid in refunded_ids:  # платёж возвращён — прокидываем флаги + меняем статус
            o = refunded_ids[oid]
            rec["refunded"] = bool(o.get("refunded"))
            rec["chargeback"] = bool(o.get("chargeback"))
            rec["status"] = "refunded"  # вон из succeeded → не считается в CVR/выручке
        out.append(rec)
    return out


_PERSIST_LOCK = threading.Lock()


def _persist(order: dict[str, Any]) -> None:
    """Сохранить заказ АТОМАРНО: временный файл + переименование.

    Прямая запись в целевой файл ломается при одновременном сохранении из двух
    потоков: оба усекают файл при открытии и пишут с нулевого смещения, и хвост
    более длинной версии остаётся за концом более короткой. На отчётах это уже
    случилось (78eab183392d), а заказ — запись о деньгах, ей тем более нельзя.
    """
    path = ORDERS_DIR / f"{order['id']}.json"
    tmp = path.with_suffix(f".json.tmp{os.getpid()}")
    try:
        with _PERSIST_LOCK:
            tmp.write_text(json.dumps(order, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def new_order(
    tariff: str,
    amount: int,
    report_id: str,
    status: str = "created",
) -> dict[str, Any]:
    order_id = uuid.uuid4().hex[:12]
    order = {
        "id": order_id,
        "tariff": tariff,
        "amount": amount,
        "report_id": report_id,
        "payment_id": None,
        "status": status,
        "mode": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    ORDERS[order_id] = order
    _persist(order)
    return order


def get_order(order_id: str) -> dict[str, Any] | None:
    rec = ORDERS.get(order_id)
    if rec is not None:
        return rec
    path = ORDERS_DIR / f"{order_id}.json"
    if path.exists():
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            ORDERS[order_id] = rec
            return rec
        except Exception:
            return None
    return None


def update_order(order_id: str, **fields: Any) -> dict[str, Any] | None:
    rec = get_order(order_id)
    if rec is None:
        return None
    rec.update(fields)
    rec["updated_at"] = _now()
    ORDERS[order_id] = rec
    _persist(rec)
    return rec


_PAID_STATUSES = {"succeeded", "bypass"}


def is_report_paid(report_id: str) -> bool:
    """Есть ли по этому отчёту оплаченный (или bypass) заказ проверки.

    Учитываем только заказы тарифов проверки (не мониторинг), чтобы оплата
    мониторинга не разблокировала отчёт.
    """
    def _match(rec: dict[str, Any]) -> bool:
        return (
            rec.get("report_id") == report_id
            and rec.get("tariff") != "monitor"
            and rec.get("status") in _PAID_STATUSES
        )
    for rec in ORDERS.values():
        if _match(rec):
            return True
    for path in ORDERS_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _match(rec):
            ORDERS[rec["id"]] = rec
            return True
    return False


def paid_order_exists(report_id: str, tariff: str) -> bool:
    """Есть ли по отчёту УЖЕ оплаченный (или bypass) заказ данного тарифа.
    Используется как гейт против двойного списания апселла (повтор из второй вкладки
    после успешной оплаты, когда флаг выполнения ещё не проставлен асинхронно)."""
    def _match(rec: dict[str, Any]) -> bool:
        return (rec.get("report_id") == report_id
                and (rec.get("tariff") or "") == tariff
                and rec.get("status") in _PAID_STATUSES)
    for rec in ORDERS.values():
        if _match(rec):
            return True
    for path in ORDERS_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _match(rec):
            ORDERS[rec["id"]] = rec
            return True
    return False


def paid_order_with_kit(report_id: str) -> bool:
    """Оплачен ли по отчёту заказ с пакетом к сделке.

    Пакет — не отдельный тариф, а флаг на объектном заказе (как bump у базового):
    человек платит один раз, цена объекта плюс доплата. Поэтому и проверяем флаг,
    а не наличие заказа с тарифом.
    """
    def _match(rec: dict[str, Any]) -> bool:
        return (rec.get("report_id") == report_id
                and bool(rec.get("kit"))
                and rec.get("status") in _PAID_STATUSES)
    for rec in ORDERS.values():
        if _match(rec):
            return True
    for path in ORDERS_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _match(rec):
            ORDERS[rec["id"]] = rec
            return True
    return False


def paid_base_variant(report_id: str) -> str:
    """Вариант A/B/C/D оплаченного базового заказа по отчёту ('' если нет).
    Нужен премиум-варианту D: ИНН-допроверка входит в пакет бесплатно."""
    def _match(rec: dict[str, Any]) -> bool:
        return (
            rec.get("report_id") == report_id
            and rec.get("tariff") == "base"
            and rec.get("status") in _PAID_STATUSES
        )
    for rec in ORDERS.values():
        if _match(rec):
            return str(rec.get("variant") or "")
    for path in ORDERS_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _match(rec):
            ORDERS[rec["id"]] = rec
            return str(rec.get("variant") or "")
    return ""


def find_by_payment_id(payment_id: str) -> dict[str, Any] | None:
    for rec in ORDERS.values():
        if rec.get("payment_id") == payment_id:
            return rec
    for path in ORDERS_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("payment_id") == payment_id:
            ORDERS[rec["id"]] = rec
            return rec
    return None
