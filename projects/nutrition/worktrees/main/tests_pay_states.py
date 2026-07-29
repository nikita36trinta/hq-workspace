"""Экран возврата с оплаты: что показываем при каком статусе платежа.

Логика денежная, а веток шесть, и большинство из них по HTTP не воспроизвести —
нужен ответ ЮKassa. Поэтому подменяем два обращения наружу и проверяем прямо
вызовом обработчика.

Зачем: страница показывала «Оплата получена!» во ВСЕХ случаях, кроме явного
`canceled`. Человек закрывал окно оплаты (статус остаётся `pending`), возвращался
и читал, что деньги получены. То же самое было при недоступном API и при
ненайденном заказе. Утверждать списание, ничего о нём не зная, — худший вид
ошибки в оплате, поэтому ветки закреплены тестом.

Запуск (без pytest — нужны только зависимости приложения, поэтому проще всего
одноразовым контейнером; в прод-образ файл намеренно не попадает, Dockerfile
копирует только рабочие файлы):

    docker build -t nutriplan-local .
    docker run --rm -v "$PWD/tests_pay_states.py:/app/tests_pay_states.py" \
        nutriplan-local python3 /app/tests_pay_states.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATA_DIR", "/tmp/pay-states-test")

import app as A  # noqa: E402

CASES = [
    ("succeeded", "Оплата получена!"),
    ("pending", "Платёж обрабатывается"),
    ("waiting_for_capture", "Платёж обрабатывается"),
    ("canceled", "Оплата не прошла"),
    ("", "Не нашли этот платёж"),          # API молчит / платёж не найден
    ("что-то новое", "Не нашли этот платёж"),  # неизвестный статус — не угадываем
]


def _title(resp) -> str:
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", resp.body.decode())
    return m.group(1) if m else "(нет заголовка)"


def _has(resp, text: str) -> bool:
    return text in resp.body.decode()


def main() -> int:
    A._find_order = lambda oid: {"payment_id": "pid-fake"} if oid == "ord1" else {}
    failed = 0

    for status, expected in CASES:
        A._yk_get_payment = lambda pid, s=status: {"status": s}
        got = _title(A.pay_success("ord1"))
        ok = got == expected
        failed += 0 if ok else 1
        print(f"{'ok ' if ok else 'FAIL'} {status or '(пусто)':<22} -> {got}")

    # заказа нет вовсе — статус платежа не важен
    A._yk_get_payment = lambda pid: {"status": "succeeded"}
    got = _title(A.pay_success("nosuchorder"))
    ok = got == "Не нашли этот платёж"
    failed += 0 if ok else 1
    print(f"{'ok ' if ok else 'FAIL'} {'нет заказа':<22} -> {got}")

    # Из «обрабатывается» должен быть выход: назад к тому же платежу, пока
    # ссылка ЮKassa жива, и запасной путь, когда её нет.
    A._find_order = lambda oid: {"payment_id": "pid", "pay_url": "https://pay.example/xyz"}
    A._yk_get_payment = lambda pid: {"status": "pending"}
    r = A.pay_success("ord1")
    for label, cond in (("кнопка «Вернуться к оплате»", _has(r, "Вернуться к оплате")),
                        ("ссылка «Оформить заново»", _has(r, "Оформить заново"))):
        failed += 0 if cond else 1
        print(f"{'ok ' if cond else 'FAIL'} pending: {label}")

    # Без сохранённой ссылки кнопки быть не должно — вести некуда.
    A._find_order = lambda oid: {"payment_id": "pid"}
    r = A.pay_success("ord1")
    cond = not _has(r, "Вернуться к оплате") and _has(r, "Оформить заново")
    failed += 0 if cond else 1
    print(f"{'ok ' if cond else 'FAIL'} pending без pay_url: только «Оформить заново»")

    # На успехе никаких «вернуться» быть не должно.
    A._find_order = lambda oid: {"payment_id": "pid", "pay_url": "https://pay.example/xyz"}
    A._yk_get_payment = lambda pid: {"status": "succeeded"}
    r = A.pay_success("ord1")
    cond = not _has(r, "Вернуться к оплате")
    failed += 0 if cond else 1
    print(f"{'ok ' if cond else 'FAIL'} succeeded: кнопки возврата нет")

    print("FAILED" if failed else "OK: все ветки возврата с оплаты честны")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
