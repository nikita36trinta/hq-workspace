"""
Единый клиент NewDB (api.newdb.net/v2) для всех проверок физлица.

Асинхронный API: POST submit → polling по requestId до state=complete.
Один токен (NEWDB_TOKEN) покрывает: ФССП, банкротство, паспорт+ИНН, залоги,
арбитраж. Тарификация pay-per-request; опрос результата не тарифицируется.

Методы (см. https://newdb.net/docs/fiz/):
- fssp_person   — исполнительные производства (ФИО+dob+regioncode)
- passport_fns  — действительность паспорта + ИНН (ФИО+dob+seria+nomer)
- bankrot_person— банкротство (innfiz)
- arbitr_person — арбитражные дела (innfiz)
- pledge_person — залоги/обременения (ФИО)
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

NEWDB_URL = "https://api.newdb.net/v2"

# Журнал ПЛАТНЫХ вызовов NewDB — каждый submit тарифицируется. Отвечает на вопрос
# «куда ушли деньги с баланса»: метод, время, успех. Опрос результата не логируем (бесплатен).
_CALL_LOG = Path(os.getenv("NEWDB_LOG_DIR", "/app/data")) / "newdb_calls.jsonl"


def _log_call(method: str, ok: bool, err: str = "", order: str = "") -> None:
    try:
        _CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_CALL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "method": method,
                                "ok": ok, "err": (err or "")[:80], "order": order}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


class NewDBError(Exception):
    """Ошибка запроса NewDB (нет токена / ошибка API / таймаут)."""


def token() -> str:
    return os.getenv("NEWDB_TOKEN", "").strip()


# Кэш баланса. Раньше комментарий в app.py обещал «баланс кэширован, проверка
# дешёвая» — кэша не было, и живой GET с таймаутом 10с висел в горячем пути
# КАЖДОГО нажатия «Оплатить» и каждого превью. 60с достаточно: баланс меняется
# только нашими же запросами, а всплеск оплат перестаёт бить по /v2/balance.
_BAL_CACHE: dict[str, Any] = {"value": None, "at": 0.0}
_BAL_TTL = 60.0


def balance(max_age: float = _BAL_TTL) -> int | None:
    """Текущий баланс аккаунта NewDB (GET /v2/balance). None при ошибке/без токена.

    None означает «НЕ ЗНАЕМ», а не «всё хорошо»: нет токена, 4xx/5xx, таймаут,
    нечисловой ответ. Вызывающий код обязан трактовать None как отказ — иначе
    предохранитель отключается ровно тогда, когда провайдер лёг (инцидент 25.07:
    клиенты платили, пока NewDB не отвечал, и получали пустые отчёты).

    Ошибка кэшируется на 10с, а не на минуту: провайдер поднялся — узнаем быстро.
    """
    now = time.time()
    if _BAL_CACHE["value"] is not None and now - _BAL_CACHE["at"] < max_age:
        return int(_BAL_CACHE["value"])
    if _BAL_CACHE["value"] is None and now - _BAL_CACHE["at"] < min(10.0, max_age):
        return None
    tok = token()
    if not tok:
        return None
    val: int | None = None
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{NEWDB_URL}/balance", headers={"X-API-KEY": tok})
            if r.status_code < 400:
                val = int(float(r.json().get("balance")))  # приходит и "412.50"
    except Exception:  # noqa: BLE001
        val = None
    _BAL_CACHE["value"], _BAL_CACHE["at"] = val, now
    return val


class NewDBTimeout(NewDBError):
    """Запрос не успел завершиться за отведённое время (флаки-latency источника)."""


class NewDBSchemaError(NewDBError):
    """Ответ пришёл, но не той формы, что мы умеем читать.

    Отдельный тип, потому что это не «у человека ничего не найдено», а «мы не
    знаем, что у человека» — и наверх это должно уйти как «не проверено», плюс
    громкий ALERT: молчаливая поломка провайдера иначе превращается в конвейер
    ложных «рисков не обнаружено». Ретраить бесполезно — схема не починится."""


# Незабранные результаты: requestId оплаченных запросов, которые мы не дождались.
# NewDB асинхронен и БЕЗ верхней границы: submit тарифицируется сразу, а источник
# может ответить и через 20 минут — тогда результат ложится под тот же requestId
# и лежит там. Опрос по requestId БЕСПЛАТЕН.
#
# Раньше requestId выбрасывался вместе с таймаутом, и вернуться за уже оплаченным
# ответом было невозможно — мы платили за то же самое заново. 25.07 один объект
# (77:05:0005005:4879) оплатили трижды; первые два запроса дали идентичный ответ,
# оба выброшены.
_DATA_DIR = Path(os.getenv("NEWDB_LOG_DIR", "/app/data"))
_PENDING_FILE = _DATA_DIR / "newdb_pending.json"      # ждём ответа
_RECOVERED_FILE = _DATA_DIR / "newdb_recovered.json"  # ответ забран, лежит бесплатно
_STORE_LOCK = threading.Lock()

# Сколько живёт забранный результат, прежде чем считать его устаревшим. Сутки:
# ЕГРН и ФССП меняются не поминутно, а повторный прогон отчёта в тот же день
# должен быть бесплатным. Дольше держать нельзя — продаём актуальность.
_RECOVERED_TTL = 24 * 3600
# Через сколько бросаем ждать незабранный ответ. Неделя с запасом: их внутренний
# restart по наблюдениям укладывается в десятки минут.
_PENDING_TTL = 7 * 24 * 3600


def _key(method: str, params: dict[str, Any]) -> str:
    """Стабильный ключ запроса: один и тот же вопрос -> один ключ."""
    norm = {k: str(v).strip().lower() for k, v in sorted(params.items())
            if k != "country" and v not in (None, "")}
    return method + "|" + json.dumps(norm, ensure_ascii=False, sort_keys=True)


def _load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — нет файла / битый: начинаем с чистого
        return {}


def _save(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)   # атомарно: сборщик и прогон отчёта пишут параллельно
    except Exception:  # noqa: BLE001
        pass


def remember_pending(method: str, params: dict[str, Any], request_id: str,
                     ref: str = "") -> None:
    """Запомнить оплаченный, но не дождавшийся ответа запрос — чтобы забрать даром."""
    if not request_id:
        return
    with _STORE_LOCK:
        pend = _load(_PENDING_FILE)
        pend[request_id] = {
            "ts": time.time(), "method": method, "key": _key(method, params),
            "ref": ref or str(params.get("address") or params.get("lastname") or ""),
        }
        _save(_PENDING_FILE, pend)


def cached(method: str, params: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Уже оплаченный и забранный ответ на ЭТОТ ЖЕ вопрос, если он свежий.

    Смысл: отчёт, упавший по таймауту, после появления результата
    перезапускается БЕСПЛАТНО — мы не покупаем второй раз то, что купили."""
    with _STORE_LOCK:
        rec = _load(_RECOVERED_FILE).get(_key(method, params))
    if not rec:
        return None
    if time.time() - rec.get("ts", 0) > _RECOVERED_TTL:
        return None
    rows = rec.get("rows")
    return rows if isinstance(rows, list) else None


def sweep_pending(limit: int = 25) -> dict[str, int]:
    """Забрать готовые результаты по сохранённым requestId. ПОЛНОСТЬЮ БЕСПЛАТНО.

    Это единственная фоновая работа, которой разрешено ходить в NewDB: опрос по
    requestId не тарифицируется. Ничего не отправляет — только забирает уже
    оплаченное. Возвращает счётчики для лога."""
    with _STORE_LOCK:
        pend = _load(_PENDING_FILE)
    if not pend:
        return {"ready": 0, "waiting": 0, "dropped": 0}

    ready = waiting = dropped = 0
    now = time.time()
    got: dict[str, Any] = {}
    done: list[str] = []
    for rid, info in list(pend.items())[:limit]:
        if now - info.get("ts", 0) > _PENDING_TTL:
            done.append(rid)
            dropped += 1
            continue
        try:
            rows = fetch(rid, info.get("method", ""))
        except NewDBTimeout:
            waiting += 1
            continue
        except NewDBError as e:  # noqa: BLE001 — failed/схема: ждать больше нечего
            print(f"[pending] {rid[:8]} снят: {e}", flush=True)
            done.append(rid)
            dropped += 1
            continue
        got[info.get("key", rid)] = {"ts": now, "rows": rows,
                                     "method": info.get("method", ""),
                                     "request_id": rid, "ref": info.get("ref", "")}
        done.append(rid)
        ready += 1
        print(f"[pending] забран даром: {info.get('method')} {info.get('ref','')[:40]} "
              f"({len(rows)} зап.)", flush=True)

    if got or done:
        with _STORE_LOCK:
            rec = _load(_RECOVERED_FILE)
            rec.update(got)
            # чистим протухшее, чтобы файл не рос вечно
            rec = {k: v for k, v in rec.items() if now - v.get("ts", 0) <= _RECOVERED_TTL}
            _save(_RECOVERED_FILE, rec)
            pend2 = _load(_PENDING_FILE)
            for rid in done:
                pend2.pop(rid, None)
            _save(_PENDING_FILE, pend2)
    return {"ready": ready, "waiting": waiting, "dropped": dropped}


def pending_stats() -> dict[str, Any]:
    """Для админки: сколько оплаченных ответов ждём и сколько уже забрали."""
    with _STORE_LOCK:
        pend, rec = _load(_PENDING_FILE), _load(_RECOVERED_FILE)
    now = time.time()
    return {
        "waiting": len(pend),
        "recovered": sum(1 for v in rec.values() if now - v.get("ts", 0) <= _RECOVERED_TTL),
        "oldest_min": int((now - min((v.get("ts", now) for v in pend.values()), default=now)) / 60),
        "items": [{"ref": v.get("ref", ""), "method": v.get("method", ""),
                   "min": int((now - v.get("ts", now)) / 60)} for v in pend.values()],
    }


def fetch(request_id: str, method: str, timeout: float = 30.0) -> list[dict[str, Any]]:
    """Забрать результат ранее оплаченного запроса по requestId. БЕСПЛАТНО.

    Бросает NewDBTimeout, если ответ ещё не готов (state != complete) — значит
    источник всё ещё работает, надо зайти позже, а не платить снова."""
    tok = token()
    if not tok:
        raise NewDBError("не настроен доступ к базе (NEWDB_TOKEN)")
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(NEWDB_URL, json={"requestId": request_id},
                               headers={"X-API-KEY": tok, "Content-Type": "application/json"})
            if resp.status_code >= 400:
                raise NewDBError(f"HTTP {resp.status_code}")
            data = resp.json()
    except NewDBError:
        raise
    except Exception as e:  # noqa: BLE001
        raise NewDBError(f"{type(e).__name__}") from e
    state = data.get("state")
    if state == "failed":
        raise NewDBError((data.get("errors_info") or [{}])[0].get("error", "источник вернул ошибку"))
    if state != "complete":
        # queued / in progress / restart — их внутренний перезапуск ещё идёт.
        raise NewDBTimeout(f"ещё не готов (state={state})")
    return _extract(method, data)


def _attempt(method: str, params: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    """Один submit+poll. Бросает NewDBTimeout при недоборе времени,
    NewDBError — при явной ошибке API (баланс/токен/валидация/failed)."""
    tok = token()
    if not tok:
        raise NewDBError("не настроен доступ к базе (NEWDB_TOKEN)")
    headers = {"X-API-KEY": tok, "Content-Type": "application/json"}
    submit = {
        "params": {"country": "ru", "method": method, **params},
        "requestId": str(uuid.uuid4()),
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(NEWDB_URL, json=submit, headers=headers)
            # Раньше статус не смотрели вовсе: ответ 500 с валидным JSON без
            # errors_info проходил как принятый (и оплаченный) submit.
            if resp.status_code >= 400:
                _log_call(method, False, f"HTTP {resp.status_code}")
                raise NewDBError(f"HTTP {resp.status_code}")
            data = resp.json()
            if data.get("errors_info"):
                err = data["errors_info"][0].get("error", "ошибка запроса")
                _log_call(method, False, err)  # submit отклонён — обычно НЕ тарифицируется
                raise NewDBError(err)
            rid, state = data.get("requestId"), data.get("state")
            _log_call(method, True)  # submit принят → запрос тарифицирован (списание с баланса)
            deadline = time.time() + timeout
            while state not in ("complete", "failed") and time.time() < deadline:
                time.sleep(3.0)
                data = client.post(
                    NEWDB_URL, json={"requestId": rid}, headers=headers
                ).json()
                state = data.get("state")
            if state == "failed":
                err = (data.get("errors_info") or [{}])[0].get("error", "источник вернул ошибку")
                raise NewDBError(err)
            # Если статус complete пришёл сразу на submit, полезной нагрузки в
            # этом ответе может не быть — её отдаёт отдельный запрос по requestId.
            # Раньше это скрывалось: пустой разбор молча превращался в «чисто».
            if state == "complete" and not isinstance(data.get("results"), dict):
                data = client.post(
                    NEWDB_URL, json={"requestId": rid}, headers=headers
                ).json()
                state = data.get("state") or state
    except NewDBError:
        raise
    except Exception as e:  # сеть / json
        raise NewDBError(f"{type(e).__name__}") from e
    if state != "complete":
        # Запрос ОПЛАЧЕН и продолжает выполняться на их стороне. Запоминаем
        # requestId: результат доедет и его можно будет забрать бесплатно.
        remember_pending(method, params, str(rid or ""))
        raise NewDBTimeout(f"сервис не успел вернуть результат (requestId={rid})")
    return _extract(method, data)


def _extract(method: str, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Достать result.data, ОТЛИЧАЯ «записей нет» от «схема поехала».

    Раньше здесь стояла цепочка `... or {} ... or []`, которая не могла упасть:
    что бы ни пришло, на выходе был список, в худшем случае пустой. А пустой
    список выше по коду означает «чисто» — «исполнительных производств не
    найдено», «банкротств нет», RISK_LOW.

    То есть если провайдер молча переименует поле или отдаст ошибку в непривычной
    форме, сервис не сломается заметно: он продолжит брать деньги и выдавать
    юридические заключения «рисков не обнаружено» про людей, которых никто
    не проверял. Для проверки чистоты сделки это самый дорогой класс ошибки —
    дороже любых лишних списаний.

    Поэтому: отсутствие ожидаемого узла — это NewDBError («не проверено» + алерт),
    и только реальный пустой список означает «записей нет».
    """
    results = data.get("results")
    if not isinstance(results, dict) or method not in results:
        raise NewDBSchemaError(f"нет results.{method} в ответе")
    node = results.get(method)
    if not isinstance(node, dict):
        raise NewDBSchemaError(f"results.{method} не объект")
    result = node.get("result")
    if not isinstance(result, dict):
        # Некоторые методы при пустой выдаче отдают result: null — это законное
        # «ничего не найдено», а не поломка схемы.
        if result is None:
            return []
        raise NewDBSchemaError(f"results.{method}.result не объект")
    rows = result.get("data")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise NewDBSchemaError(f"results.{method}.result.data не список")
    return rows


def call(method: str, params: dict[str, Any], timeout: float = 120.0,
         attempts: int = 1) -> list[dict[str, Any]]:
    """Выполнить метод NewDB и вернуть result.data (список записей).

    Источник (Росреестр/ФССП) флаки по latency: один и тот же запрос то ~40с,
    то >90с. Поэтому per-attempt таймаут держим умеренным, а на ТАЙМАУТ делаем
    retry со свежим requestId — новый запрос обычно попадает в быстрый путь.
    На явную ошибку API (баланс/токен/валидация/failed) НЕ ретраим — бесполезно.

    Бросает NewDBError — вызывающий код честно помечает проверку "не проверено".
    """
    # СНАЧАЛА смотрим, не забрали ли мы уже ответ на этот же вопрос даром.
    # Отчёт, упавший вчера по таймауту, перезапускается бесплатно — мы не
    # покупаем второй раз то, за что уже заплатили.
    hit = cached(method, params)
    if hit is not None:
        print(f"[pending] {method}: ответ уже оплачен ранее — берём из забранных, "
              f"новых списаний нет", flush=True)
        return hit

    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return _attempt(method, params, timeout)
        except NewDBSchemaError as e:
            # Громко: провайдер сменил формат, и мы платим за ответы, которые не
            # умеем прочесть. Без этого поломка была бы неотличима от «всё чисто».
            print(f"[ALERT] NewDB схема ответа изменилась: method={method}: {e} — "
                  f"проверки уходят как «не проверено», нужен разбор", flush=True)
            raise
        except NewDBTimeout as e:
            last = e
            continue  # свежий requestId на следующей итерации
        # прочие NewDBError (баланс/failed/сеть) — ретрай не поможет
    raise last or NewDBError("сервис не успел вернуть результат")
