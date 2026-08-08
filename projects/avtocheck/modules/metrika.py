"""
Offline-конверсии в Яндекс.Метрику по ClientId (_ym_uid).

Зачем: цель pay_success срабатывает ПОСЛЕ возврата с ЮKassa — Метрика
записывает это как «прямой заход» и теряет привязку к рекламному клику.
Загружая конверсию оффлайн по ClientId, мы отдаём Метрике «кто оплатил»,
и она сама сопоставляет с историей пользователя (включая клик по Директу).
Так Директ корректно засчитывает конверсию кампании + работает оптимизация.

Docs: https://yandex.ru/dev/metrika/doc/api2/practice/offline-conversions.html
"""
from __future__ import annotations

import os
import time

import httpx

_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter}/offline_conversions/upload"


def upload_pay_conversion(ym_uid: str, price: float,
                          target: str = "pay_success", when_ts: int | None = None) -> bool:
    """Загрузить одну offline-конверсию оплаты. Fire-and-forget (не падаем)."""
    token = os.getenv("YANDEX_METRIKA_TOKEN", "").strip()
    counter = os.getenv("METRIKA_COUNTER_ID", "").strip()
    ym_uid = (ym_uid or "").strip()
    if not (token and counter and ym_uid):
        return False
    ts = int(when_ts or time.time())
    csv = ("ClientId,Target,DateTime,Price,Currency\n"
           f"{ym_uid},{target},{ts},{float(price or 0):.2f},RUB\n")
    try:
        with httpx.Client(timeout=30) as c:
            r = c.post(
                _URL.format(counter=counter),
                params={"client_id_type": "CLIENT_ID"},
                headers={"Authorization": f"OAuth {token}"},
                files={"file": ("conv.csv", csv.encode("utf-8"), "text/csv")},
            )
        return r.status_code < 400
    except Exception:
        return False


def client_id_from_cookie(ym_uid_cookie: str | None) -> str:
    """_ym_uid и есть ClientId Метрики (значение куки — число). Возвращаем как есть."""
    return (ym_uid_cookie or "").strip()


_STAT_URL = "https://api-metrika.yandex.net/stat/v1/data"


def fetch_engagement(date1: str = "today", date2: str = "today") -> dict | None:
    """Сводка вовлечённости из Метрики за период: визиты, пользователи,
    отказы (%), среднее время на сайте (сек), глубина (страниц/визит).
    None при отсутствии токена/счётчика или ошибке — страница просто не покажет блок."""
    token = os.getenv("YANDEX_METRIKA_TOKEN", "").strip()
    counter = os.getenv("METRIKA_COUNTER_ID", "").strip()
    if not (token and counter):
        return None
    try:
        with httpx.Client(timeout=25) as c:
            r = c.get(
                _STAT_URL,
                headers={"Authorization": f"OAuth {token}"},
                params={
                    "ids": counter, "date1": date1, "date2": date2,
                    "metrics": ("ym:s:visits,ym:s:users,ym:s:bounceRate,"
                                "ym:s:avgVisitDurationSeconds,ym:s:pageDepth"),
                    "accuracy": "full",
                },
            )
        if r.status_code >= 400:
            return None
        t = (r.json().get("totals") or [0, 0, 0, 0, 0])
        return {
            "visits": int(t[0]), "users": int(t[1]),
            "bounce_rate": round(float(t[2]), 1),
            "avg_seconds": int(t[3]), "page_depth": round(float(t[4]), 2),
        }
    except Exception:
        return None
