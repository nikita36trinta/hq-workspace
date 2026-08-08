"""Клиент apipoint.ru — данные об автомобиле по VIN и госномеру.

Почему именно этот поставщик (обзор рынка 07.08.2026): единственный из
проверенных, кто по оферте заключает договор с физлицом, не берёт абонплату и
минимальный месячный платёж и возвращает остаток аванса по заявлению. У api-cloud
регистрация только для юрлиц плюс автосписание 700 ₽/мес при обороте до 2000
запросов. У api-assist автомобильный раздел есть, но цены непубличны, а вход
только по VIN — госномер не принимается.

Главное свойство API, на котором держится весь модуль: В КАЖДОМ ОТВЕТЕ приходят
`price` и `balance`. Значит расход виден по каждому вызову, и гадать о нём не
нужно — мы пишем и то, и другое в журнал.

Устройство API: одна точка входа, метод выбирается полем `sources`.

    POST https://apipoint.ru/api/call
    Authorization: Bearer <токен>
    {"sources": "vindecode", "vin": "..."}
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

URL = "https://apipoint.ru/api/call"

# Журнал ПЛАТНЫХ вызовов: метод, цена, остаток. Отвечает на вопрос «куда ушли
# деньги» и заодно даёт фактическую себестоимость отчёта, а не расчётную.
LOG_PATH = Path(os.getenv("APIPOINT_LOG", str(Path.home() / ".config/digiterium/apipoint_calls.jsonl")))

# Цены из документации на 07.08.2026 — для смет и проверки, что поставщик не
# поднял тариф молча. Источник истины всё равно поле `price` в ответе.
PRICES: dict[str, float] = {
    "dtp": 0.60, "numberrecognize": 0.80, "probeg": 1.10, "eaisto": 1.10,
    "vindecode": 1.10, "zalog": 1.10, "carprices": 1.10, "offerbygosnum": 1.10,
    "elpts": 1.20, "number2vin": 1.20, "gost": 1.30, "nomerogram": 1.30,
    "fts": 1.40, "taxi": 1.50, "autophoto": 1.60, "bidcars": 1.70,
    "offerbyvin": 1.80, "notary": 1.90, "fedresurs": 1.90, "gibdddtp": 2.00,
    "eaistobyvinorgosnum": 2.00, "leasing": 2.10, "probeg2": 2.10,
    "gibddhistory": 2.10, "getfineinfo": 2.10, "customs": 2.10,
    "utilization": 2.10, "gibddhistory2": 2.10, "priceapi": 2.10,
    "vindecode2": 3.20, "zalogvin": 3.20, "avgcarprice": 3.20, "fsspdata": 3.20,
    "avgpricebyvin": 4.20, "getpts": 5.30, "carshering": 6.00, "vin2number": 6.00,
    "frameapi": 10.50, "vinbynumber": 12.00, "regperiods": 18.90, "gai": 21.00,
    "fullapi": 25.00, "servicemaintenance": 26.30, "reportjson": 50.00,
}


class ApiPointError(Exception):
    """Ошибка вызова apipoint."""


class NoFunds(ApiPointError):
    """Баланс исчерпан. Отдельный класс: это не сбой, а ожидаемое состояние,
    и обрабатывать его надо иначе — приостановить проверки, а не ретраить."""


def token() -> str:
    tok = os.getenv("APIPOINT_TOKEN", "").strip()
    if tok:
        return tok
    # Токен лежит рядом с ключами Директа, права 600.
    env = Path.home() / ".config/digiterium/apipoint.env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("APIPOINT_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _log(method: str, price: float | None, balance: float | None,
         ok: bool, err: str = "") -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "method": method, "price": price, "balance": balance,
                "ok": ok, "err": err[:120],
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — журнал не имеет права ронять проверку
        pass


def call(method: str, params: dict[str, Any] | None = None,
         timeout: float = 60.0, attempts: int = 2) -> dict[str, Any]:
    """Вызов метода. Возвращает ПОЛНЫЙ ответ, включая price и balance.

    Повторяем только сетевые сбои и 5xx. Отказ по балансу и ошибку валидации не
    повторяем: первое требует денег, второе — исправления запроса, и в обоих
    случаях повтор просто тратит время.
    """
    tok = token()
    if not tok:
        raise ApiPointError("не настроен APIPOINT_TOKEN")
    body = {"sources": method, **(params or {})}
    headers = {"Authorization": f"Bearer {tok}",
               "Content-Type": "application/json", "Accept": "application/json"}
    last = ""
    for i in range(max(1, attempts)):
        try:
            r = httpx.post(URL, json=body, headers=headers, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            if i + 1 < attempts:
                time.sleep(0.8 * (i + 1))
            continue
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            _log(method, None, None, False, "ответ не JSON")
            raise ApiPointError("ответ не JSON") from e
        if not isinstance(data, dict):
            _log(method, None, None, False, "неожиданная структура")
            raise ApiPointError("неожиданная структура ответа")

        msg = str(data.get("message") or "")
        bal = _num(data.get("balance"))
        if "едостаточно средств" in msg:
            _log(method, None, bal, False, msg)
            raise NoFunds(msg)
        if data.get("errors"):
            err = "; ".join(str(x) for x in data["errors"])
            _log(method, None, bal, False, err)
            raise ApiPointError(err)
        if r.status_code >= 500:
            last = f"HTTP {r.status_code}"
            if i + 1 < attempts:
                time.sleep(0.8 * (i + 1))
            continue
        if r.status_code >= 400:
            _log(method, None, bal, False, msg or f"HTTP {r.status_code}")
            raise ApiPointError(msg or f"HTTP {r.status_code}")

        _log(method, _num(data.get("price")), bal, True)
        return data
    _log(method, None, None, False, last)
    raise ApiPointError(last or "нет ответа")


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def balance() -> float | None:
    """Остаток. Отдельного метода баланса у API нет — берём из ответа самого
    дешёвого вызова. При нулевом балансе отказ тоже несёт остаток, поэтому
    цифру получаем даже когда денег нет."""
    try:
        return _num(call("dtp", {"vin": "X" * 17}, attempts=1).get("balance"))
    except NoFunds as e:
        return _num(str(e).split()[-1]) if str(e)[-1].isdigit() else 0.0
    except ApiPointError:
        return None


def last_balance() -> float | None:
    """Остаток из журнала вызовов — БЕСПЛАТНО и без похода в сеть.

    balance() выше стоит 0,60 ₽ за замер, потому что у поставщика нет метода
    баланса и цифру приходится добывать платным вызовом. Но остаток приходит в
    ответе КАЖДОГО вызова и уже записан в журнал, а один отчёт — это восемь
    вызовов. Поэтому там, где цифра нужна часто (гейт на оплате), берём
    последнюю записанную: под трафиком она отстаёт на секунды.

    None — журнала нет или в нём ни одной записи с остатком (первый запуск).
    """
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-400:]):
        try:
            bal = json.loads(line).get("balance")
        except ValueError:
            continue
        if bal is not None:
            return _num(bal)
    return None


# ── прикладные обёртки ──────────────────────────────────────────────────────

def identify(vin: str = "", plate: str = "") -> dict[str, Any]:
    """Опознать машину: марка, модель, год, двигатель.

    По VIN — один вызов за 1,10 ₽. По госномеру дороже: сначала конвертация
    (1,20 ₽), потом декодер, итого 2,30 ₽. Госномер не принимает ни один
    поставщик на рынке, так что это не наша особенность, а свойство рынка —
    и повод просить в форме именно VIN.
    """
    vin = (vin or "").strip().upper()
    if not vin:
        if not plate:
            raise ApiPointError("нужен VIN или госномер")
        got = call("number2vin", {"gosnomer": plate.strip().upper()})
        vin = str((got.get("result") or {}).get("vin") or "").strip().upper()
        if not vin:
            return {"found": False, "why": "по госномеру VIN не найден"}
    out = call("vindecode", {"vin": vin})
    res = out.get("result") or {}
    return {"found": bool(res), "vin": vin, "data": res,
            "price": _num(out.get("price")), "balance": _num(out.get("balance"))}


def risk_signals(vin: str) -> dict[str, Any]:
    """Дешёвые сигналы для превью: было ли ДТП и есть ли залог. 1,70 ₽ на двоих.

    Именно на этом Автокод строит бесплатный крючок «найден минимум 1 недостаток»:
    факт риска показывают даром, содержание прячут за оплатой.
    """
    out: dict[str, Any] = {}
    for method in ("dtp", "zalog"):
        try:
            out[method] = call(method, {"vin": vin}).get("result")
        except NoFunds:
            raise
        except ApiPointError as e:
            out[method] = {"error": str(e)}
    return out


def spent_today() -> float:
    """Сколько потрачено за сегодня по журналу — для сверки со счётом."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    if not LOG_PATH.exists():
        return 0.0
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if str(e.get("ts", ""))[:10] == today and e.get("price"):
            total += float(e["price"])
    return round(total, 2)
