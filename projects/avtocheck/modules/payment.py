"""
ЮKassa API v3 — реальная интеграция без SDK, только httpx.

Стратегия:
  - Есть YOOKASSA_SHOP_ID + YOOKASSA_SECRET_KEY → реальный POST к
    https://api.yookassa.ru/v3/payments с Basic-auth и Idempotence-Key.
    Один и тот же код работает и для тест-магазина, и для боевого —
    режим определяется исключительно тем, какие ключи выданы в ЮKassa.
  - Ключей нет → честный bypass-режим: платёж не создаётся, но флоу
    проходит до конца (mode="bypass"), чтобы демо запускалось без .env.

Никаких зашитых ключей. Секреты читаются ТОЛЬКО из окружения.
Спецификация API: https://yookassa.ru/developers/api
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


# amount — ДЕФОЛТ для чека; реальная сумма всегда перебивается amount_override из app.py
# (цена варианта A/B/C). Дефолты держим = вариант B, чтобы новый вход без override не ушёл
# в старую цену. base/object/addon_seller варьируются по A/B/C в app.py (раунд 3, 2026-07-24).
# Названия попадают в описание платежа и в чек 54-ФЗ, который клиент видит в
# банке и в почте. «Проверка объекта недвижимости» в чеке за проверку машины —
# первая причина спорить с банком и требовать возврат. Суммы здесь остаются
# запасными: фактическую цену перебивает amount_override (PRICE_FULL/PRICE_OBJECT).
TARIFFS = {
    "base": {"title": "Полная проверка автомобиля", "amount": 449},
    "ext": {"title": "Расширенная проверка", "amount": 2990},
    "realtor": {"title": "Риелторам — 10 проверок", "amount": 9900},
    "monitor": {"title": "Мониторинг продавца, 30 дней", "amount": 990},
    # Апселлы 249/250 → 199 (05.08.2026). За 24.07–04.08 ИНН взяли 9 из 52 показов
    # (17%), продавца — 5 из 114 (4%). У продавца дело не в цене, а в том, что его
    # предлагают тем, кто нажал «не знаю ФИО»; цену снижаем как второй фактор.
    "addon": {"title": "Дополнительная проверка", "amount": 199},
    "object": {"title": "Базовая проверка автомобиля", "amount": 199},
    "addon_seller": {"title": "Дополнение к отчёту", "amount": 199},
    # Доплата с базового тарифа до полного: разница 449 − 199. Отдельный тариф,
    # а не «купите ещё раз» — иначе человек платит 199 + 449 за один отчёт.
    "upgrade": {"title": "Доплата до полного отчёта по автомобилю", "amount": 250},
    # Повторная проверка залога перед передачей денег. Отдельный дешёвый запрос
    # к реестру ФНП, не полный отчёт: человеку нужен один факт «здесь и сейчас».
    "recheck": {"title": "Повторная проверка залога", "amount": 99},
}

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"
_HTTP_TIMEOUT = 15.0


class YooKassaError(RuntimeError):
    """HTTP или сетевой сбой при вызове ЮKassa API."""


@dataclass
class PaymentIntent:
    id: str
    amount_rub: int
    confirmation_url: str
    status: str  # "pending"|"waiting_for_capture"|"succeeded"|"canceled"|"bypass"
    mode: str  # "yookassa" | "bypass"
    note: str


def _creds() -> tuple[str, str] | None:
    shop_id = os.getenv("YOOKASSA_SHOP_ID", "").strip()
    secret = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
    if shop_id and secret:
        return (shop_id, secret)
    return None


def is_configured() -> bool:
    return _creds() is not None


def _resolve_tariff(tariff: str) -> tuple[str, int, str]:
    key = tariff if tariff in TARIFFS else "base"
    return key, TARIFFS[key]["amount"], TARIFFS[key]["title"]


def create_payment(tariff: str, order_id: str, return_url: str,
                    email: str = "", amount_override: int | None = None) -> PaymentIntent:
    """Создать платёж (или отдать bypass-заглушку без списания).

    amount_override — фактическая сумма списания (для A/B-теста цены):
    перебивает цену тарифа, но тариф/описание остаются прежними.
    """
    tariff, amount, title = _resolve_tariff(tariff)
    if amount_override is not None:
        amount = int(amount_override)

    creds = _creds()
    if creds is None:
        return PaymentIntent(
            id="bypass-" + uuid.uuid4().hex[:12],
            amount_rub=amount,
            confirmation_url=return_url,
            status="bypass",
            mode="bypass",
            note=(
                "ЮKassa не сконфигурирована (YOOKASSA_SHOP_ID/SECRET_KEY пусты). "
                "Реальная оплата пропущена — режим демо."
            ),
        )

    shop_id, secret = creds
    title_full = f"ЧистаяСделка: {title} (заказ {order_id})"
    body: dict[str, Any] = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": title_full,
        "metadata": {"order_id": order_id, "tariff": tariff},
    }
    # Чек покупателю (54-ФЗ): почта + item. Если магазин не настроен на чеки —
    # повторяем БЕЗ чека, чтобы оплата не сломалась.
    if email and "@" in email:
        body["receipt"] = {
            "customer": {"email": email.strip()},
            "items": [{
                "description": title[:128],
                "quantity": "1.00",
                "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                "vat_code": 1,
                "payment_mode": "full_payment",
                "payment_subject": "service",
            }],
        }

    def _post(payload: dict[str, Any]):
        headers = {"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"}
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            return client.post(f"{YOOKASSA_API_BASE}/payments",
                               auth=(shop_id, secret), headers=headers, json=payload)

    try:
        resp = _post(body)
        if resp.status_code >= 400 and "receipt" in body:
            # магазин мог не принять чек — пробуем без него
            body_no_receipt = {k: v for k, v in body.items() if k != "receipt"}
            resp = _post(body_no_receipt)
    except httpx.HTTPError as exc:
        raise YooKassaError(f"Сеть/HTTP: {exc}") from exc

    if resp.status_code >= 400:
        raise YooKassaError(
            f"ЮKassa HTTP {resp.status_code}: {resp.text[:400]}"
        )

    data = resp.json()
    conf = (data.get("confirmation") or {}).get("confirmation_url") or return_url
    return PaymentIntent(
        id=str(data.get("id") or ""),
        amount_rub=amount,
        confirmation_url=conf,
        status=str(data.get("status") or "pending"),
        mode="yookassa",
        note=f"ЮKassa: платёж {data.get('id')} в статусе {data.get('status')}",
    )


def fetch_payment(payment_id: str) -> dict[str, Any]:
    """GET /v3/payments/{id} — источник истины о статусе платежа.
    Используется /pay/return и webhook: никогда не доверяем query-параметрам
    и телу webhook, всегда сверяемся с API."""
    creds = _creds()
    if creds is None:
        raise YooKassaError(
            "ЮKassa не сконфигурирована — верификация невозможна."
        )
    shop_id, secret = creds
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.get(
                f"{YOOKASSA_API_BASE}/payments/{payment_id}",
                auth=(shop_id, secret),
            )
    except httpx.HTTPError as exc:
        raise YooKassaError(f"Сеть/HTTP: {exc}") from exc
    if resp.status_code >= 400:
        raise YooKassaError(
            f"ЮKassa HTTP {resp.status_code}: {resp.text[:400]}"
        )
    return resp.json()
