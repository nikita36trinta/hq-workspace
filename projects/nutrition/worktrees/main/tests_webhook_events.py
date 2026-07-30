"""Вебхук ЮKassa: возврат и отклонённый платёж.

Зачем: вебхук знал ровно одно событие — payment.succeeded. Возврат делается
руками в ЛК ЮKassa, и в наших данных он не менял НИЧЕГО: подписка оставалась
active с привязанной картой, и через 30 дней cron списывал деньги у того, кому
мы только что вернули. То есть собственный возврат превращался в спор по
платежу. Отклонённые банком попытки были невидимы вовсе — у платёжной воронки
не было знаменателя.

Ветки денежные и по HTTP не воспроизводятся (нужен ответ ЮKassa), поэтому
подменяем обращение наружу и зовём обработчик напрямую.

Запуск:

    docker build -t nutriplan-local .
    docker run --rm -v "$PWD/tests_webhook_events.py:/app/tests_webhook_events.py" \
        nutriplan-local python3 /app/tests_webhook_events.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = tempfile.mkdtemp(prefix="webhook-test-")
os.environ["DATA_DIR"] = DATA_DIR

import app as A  # noqa: E402


class _Req:
    """Минимальный request: обработчику нужны только json() и base_url."""

    def __init__(self, payload: dict) -> None:
        self._p = payload
        self.base_url = "https://mynutriplan.ru/"

    async def json(self) -> dict:
        return self._p


class _BG:
    def add_task(self, *a, **k) -> None:  # фоновые задачи в тесте не нужны
        pass


def _call(payload: dict) -> dict:
    return json.loads(asyncio.run(A.pay_webhook(_Req(payload), _BG())).body)


def _sub(sid: str) -> dict:
    return A._load_sub(sid)


def _orders() -> list[dict]:
    try:
        return [json.loads(x) for x in A.ORDERS.read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:  # noqa: BLE001
        return []


def _fresh_sub(sid: str = "ord-sub") -> None:
    A._save_sub({"sub_id": sid, "email": "kto@example.com", "landing": "slim", "quiz": {},
                 "payment_method_id": "card-1", "amount": 499, "status": "active",
                 "created": "2026-07-01T00:00:00+00:00", "plan_token": sid,
                 "next_charge": "2026-08-01T00:00:00+00:00",
                 "next_plan": "2026-07-08T00:00:00+00:00"})


def main() -> int:  # noqa: C901
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        failed += 0 if cond else 1
        print(f"{'ok  ' if cond else 'FAIL'} {name}{(' — ' + detail) if detail and not cond else ''}")

    # ── возврат гасит подписку и убирает карту ───────────────────────────────
    _fresh_sub()
    A._write_order({"order": "ord-sub", "type": "subscription", "payment_id": "pay-1",
                    "status": "succeeded", "email": "kto@example.com", "landing": "slim"})
    A._yk_get_payment = lambda pid: {"status": "succeeded", "paid": True,
                                     "refunded_amount": {"value": "499.00"}}
    r = _call({"event": "refund.succeeded", "object": {"id": "ref-1", "payment_id": "pay-1",
                                                       "amount": {"value": "499.00"}}})
    s = _sub("ord-sub")
    check("возврат: ответ ok", r.get("ok") is True)
    check("возврат: подписка погашена", s.get("status") == "canceled", str(s.get("status")))
    check("возврат: карта отвязана", not s.get("payment_method_id"), str(s.get("payment_method_id")))
    check("возврат: причина записана", s.get("cancel_reason") == "refund")
    check("возврат: заказ помечен refunded",
          any(o.get("status") == "refunded" and o.get("refund_id") == "ref-1" for o in _orders()))

    # cron не должен трогать погашенную подписку — это и есть смысл правки
    check("возврат: cron больше не спишет", _sub("ord-sub").get("status") != "active")

    # ── повторная доставка того же возврата ничего не ломает ─────────────────
    before = len(_orders())
    r2 = _call({"event": "refund.succeeded", "object": {"id": "ref-1", "payment_id": "pay-1",
                                                        "amount": {"value": "499.00"}}})
    check("возврат: повтор распознан как дубль", r2.get("duplicate") is True)
    check("возврат: дубль не пишет вторую запись", len(_orders()) == before)

    # ── ПОДДЕЛКА: возврата в самом платеже нет → ничего не делаем ────────────
    _fresh_sub("ord-live")
    A._write_order({"order": "ord-live", "type": "subscription", "payment_id": "pay-live",
                    "status": "succeeded", "email": "live@example.com", "landing": "slim"})
    A._yk_get_payment = lambda pid: {"status": "succeeded", "paid": True,
                                     "refunded_amount": {"value": "0.00"}}
    r3 = _call({"event": "refund.succeeded", "object": {"id": "ref-fake", "payment_id": "pay-live"}})
    check("подделка возврата отклонена", r3.get("verified") is False)
    check("подделка не погасила живую подписку", _sub("ord-live").get("status") == "active")
    check("подделка не отвязала карту", _sub("ord-live").get("payment_method_id") == "card-1")

    # ── отклонённый платёж: счётчик и статус ─────────────────────────────────
    A._yk_get_payment = lambda pid: {"status": "canceled", "metadata": {
        "type": "once", "landing": "slim", "order": "ord-once", "email": "kto@example.com"},
        "cancellation_details": {"reason": "insufficient_funds"}}
    _call({"event": "payment.canceled", "object": {"id": "pay-2"}})
    check("отказ: заказ помечен canceled",
          any(o.get("status") == "canceled" and o.get("reason") == "insufficient_funds"
              for o in _orders()))
    check("отказ: счётчик неудач вырос", A._counters().get("pay_fail_slim", 0) == 1,
          str(A._counters()))

    # ── неудачное ПРОДЛЕНИЕ уводит подписку в past_due ───────────────────────
    _fresh_sub("ord-ren")
    A._yk_get_payment = lambda pid: {"status": "canceled", "metadata": {
        "type": "sub_renew", "landing": "slim", "order": "ord-ren"},
        "cancellation_details": {"reason": "card_expired"}}
    _call({"event": "payment.canceled", "object": {"id": "pay-3"}})
    check("отказ продления: подписка в past_due", _sub("ord-ren").get("status") == "past_due",
          str(_sub("ord-ren").get("status")))
    check("отказ продления: счётчик отдельный", A._counters().get("sub_fail_slim", 0) == 1)

    # ── подделка отказа: платёж на самом деле оплачен ────────────────────────
    _fresh_sub("ord-ok")
    A._yk_get_payment = lambda pid: {"status": "succeeded", "paid": True,
                                     "metadata": {"type": "sub_renew", "order": "ord-ok"}}
    r4 = _call({"event": "payment.canceled", "object": {"id": "pay-4"}})
    check("подделка отказа отклонена", r4.get("verified") is False)
    check("подделка отказа не тронула подписку", _sub("ord-ok").get("status") == "active")

    print("\nпровалов:", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    raise SystemExit(code)
