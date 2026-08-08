"""
api-assist.com — резервный источник сведений ЕГРН по объекту.

ЗАЧЕМ. NewDB — единственная точка отказа: 25.07.2026 он держал запросы
в состоянии restart по 40 минут, и восемь оплаченных отчётов зависли.
Но главное даже не скорость: NewDB ищет по адресу хуже. Отчёт 2176aea372e0
(Калуга) сутки числился «объекта нет в ЕГРН» после 131 пустого ответа —
на деле поиск спотыкался о хвост «5 этаж» в строке адреса. api-assist по
той же строке нашёл объект, и на нём оказался ЗАПРЕТ РЕГИСТРАЦИИ.

Синхронный REST: ответ за 2-3 секунды против 40 с - 40 мин у NewDB.

ЧЕГО ЗДЕСЬ НЕТ. Долей собственности: api-assist отдаёт только тип права
(«Общая долевая собственность»), без «1/2». Поэтому долевую определяем
по типу и количеству записей — для вывода «нужны согласия всех» этого
достаточно, а точные доли остаются преимуществом NewDB.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import os
import re
import time
import threading as _threading
from typing import Any

import httpx

BASE = "https://service.api-assist.com/parser/egrn_api"
FEDRESURS = "https://service.api-assist.com/parser/fedresurs_api"


class ApiAssistError(Exception):
    """Источник не ответил или ответил не тем."""


def token() -> str:
    """Ключ доступа. Принимаем оба имени: в проде переменная была заведена как
    APIASSIST_KEY ещё до появления этого модуля."""
    return (os.getenv("APIASSIST_TOKEN", "") or os.getenv("APIASSIST_KEY", "")).strip()


def enabled() -> bool:
    return bool(token())


# Тариф «Рациональный» по сервису egrn: 7500 запросов в месяц, но НЕ БОЛЬШЕ 250 в
# сутки. Упереться в суточный потолок опаснее, чем в месячный: источник начинает
# отбивать 4xx, и молча умирает фолбэк после оплаты — тот самый, что спасает уже
# заплативших. Поэтому считаем сами и резервируем хвост под платный путь.
_DAY_FILE = os.path.join(os.getenv("NEWDB_LOG_DIR", "/app/data"), "apiassist_usage.json")
_DAY_LOCK = _threading.Lock()


def day_limit() -> int:
    try:
        return int(os.getenv("APIASSIST_DAY_LIMIT", "250"))
    except ValueError:
        return 250


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def used_today() -> int:
    """Сколько запросов к egrn мы уже потратили сегодня. 0 при любой ошибке чтения:
    счётчик — предохранитель, и он не имеет права уронить основной путь."""
    try:
        with open(_DAY_FILE, encoding="utf-8") as f:
            d = _json.load(f)
        return int(d.get("n") or 0) if d.get("day") == _today() else 0
    except Exception:  # noqa: BLE001
        return 0


def _bump_day() -> None:
    with _DAY_LOCK:
        today = _today()
        n = used_today() + 1
        try:
            os.makedirs(os.path.dirname(_DAY_FILE), exist_ok=True)
            with open(_DAY_FILE, "w", encoding="utf-8") as f:
                _json.dump({"day": today, "n": n}, f)
        except Exception:  # noqa: BLE001
            pass


def budget_ok(reserve: int) -> bool:
    """Можно ли потратить запрос, оставив reserve штук на платный путь.

    reserve — не перестраховка: за сутки после оплаты мы делаем 15-30 обращений,
    и если превью съест лимит целиком, оплативший останется без отчёта, а мы —
    с очередным застрявшим заказом."""
    return used_today() < max(0, day_limit() - max(0, reserve))


def _get(path: str, params: dict[str, str], timeout: float,
         base: str = BASE, attempts: int = 3) -> dict[str, Any]:
    """Запрос к api-assist с повтором на нестабильность источника.

    Документация прямо говорит: `success=0` — это сбой ИСТОЧНИКА, такой запрос
    «не будет учтен в статистике» и его «необходимо повторить». Мы же бросали
    исключение с первого раза, и вызывающий код (например, превью) читал это как
    «объекта нет». Повтор при этом бесплатный — лимит расходуют только ответы
    с success=1.

    Отсюда же и счётчик: крутили его на каждый запрос, включая неучтённые, —
    квота на дашборде показывала больше, чем списано на самом деле.
    """
    tok = token()
    if not tok:
        raise ApiAssistError("не настроен APIASSIST_TOKEN")
    last = "неизвестно"
    for i in range(max(1, attempts)):
        try:
            r = httpx.get(f"{base}/{path}", params={**params, "key": tok}, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise ApiAssistError(f"{type(e).__name__}") from e
        if r.status_code >= 400:
            # 403 — это лимит/ключ/подписка, повтор не поможет и только теряет время.
            raise ApiAssistError(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            raise ApiAssistError("ответ не JSON") from e
        if not isinstance(data, dict):
            raise ApiAssistError("неожиданная структура ответа")
        if data.get("success"):
            # success=1 при пустом records — законное «объект не найден», а не сбой.
            # Отличать одно от другого обязательно (см. инцидент 25.07).
            if base == BASE:
                _bump_day()
            return data
        if data.get("error"):
            # Ошибка запроса (ключ, лимит, валидация) — повторять бессмысленно.
            raise ApiAssistError(str(data["error"]))
        last = "success=0"
        if i + 1 < max(1, attempts):
            time.sleep(0.7 * (i + 1))
    raise ApiAssistError(last)


def details_by_cadastre(cad: str, timeout: float = 90.0) -> list[dict[str, Any]]:
    """Карточка объекта по кадастровому номеру. Пустой список = объекта нет."""
    data = _get("details_by_number", {"cadNumber": cad.strip()}, timeout)
    rows = data.get("records")
    if rows is None:
        raise ApiAssistError("нет поля records")
    return [r for r in rows if isinstance(r, dict)]


def search_by_address(address: str, timeout: float = 90.0) -> list[dict[str, Any]]:
    """Кадастровые номера по адресу. Пустой список = не нашли."""
    data = _get("search_by_address", {"address": address.strip()}, timeout)
    rows = data.get("records")
    if rows is None:
        raise ApiAssistError("нет поля records")
    return [r for r in rows if isinstance(r, dict)]


# ── выбор нужного объекта из адресной выдачи ────────────────────────────────
_FLAT_RE = re.compile(r"(?:кв|квартира|помещ\w*)[\s.]*№?\s*(\d+[а-яa-z]?)", re.I)


def _flat_of(text: str) -> str:
    m = _FLAT_RE.search(text or "")
    return (m.group(1) or "").lower() if m else ""


# Дом и корпус — в обоих написаниях: наш ввод («д 5 к 5») и ответ api-assist
# («д. 5, корп. 5, литера. А»). «Литеру» игнорируем: в петербургских адресах она
# есть почти всегда, а в пользовательском вводе — почти никогда.
_HOUSE_RE = re.compile(r"(?:^|[\s,])(?:д|дом|влад\w*|вл)[\s.]*№?\s*(\d+[а-я]?(?:[-/]\d+[а-я]?)?)", re.I)
_BLDG_RE = re.compile(r"(?:^|[\s,])(?:к|корп\w*)[\s.]*№?\s*(\d+[а-я]?)", re.I)


def _house_of(text: str) -> str:
    m = _HOUSE_RE.search(text or "")
    return (m.group(1) or "").lower().replace(" ", "") if m else ""


def _bldg_of(text: str) -> str:
    m = _BLDG_RE.search(text or "")
    return (m.group(1) or "").lower() if m else ""


def pick_object(rows: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Выбрать объект из адресной выдачи. None — если выбрать НЕЛЬЗЯ.

    Угадывать здесь опаснее, чем не ответить: по «Московское шоссе 7к2»
    возвращается 29 объектов, а по «ул. Комарова 11» — четыре, включая два
    земельных участка. Отдать клиенту отчёт про чужую квартиру хуже, чем
    попросить его уточнить номер.

    Сравниваем ДОМ + КОРПУС + КВАРТИРУ. Раньше смотрели только квартиру, и на
    «Дачный пр-кт, д 5 к 5, кв 24» выдача давала шестнадцать «квартир 24» — в
    домах 5, 9, 16, 19, 21 и в корпусе 4. Функция видела неоднозначность и
    отказывалась, хотя нужная строка там была ровно одна: по дому и корпусу она
    отличается однозначно. Заодно это страхует от обратной ошибки — когда чужой
    дом оказывался единственным совпадением по номеру квартиры.
    """
    if not rows:
        return None
    want_flat, want_house, want_bldg = _flat_of(query), _house_of(query), _bldg_of(query)
    if len(rows) == 1:
        # Единственный ответ тоже проверяем: «одна строка» сама по себе ничего не
        # доказывает. Тест поймал ровно это — по запросу «кв 24» единственным
        # ответом приходила кв. 88 в том же доме и молча уходила клиенту.
        addr = str(rows[0].get("address") or "")
        for want, got in ((want_house, _house_of(addr)),
                          (want_bldg, _bldg_of(addr)),
                          (want_flat, _flat_of(addr))):
            if want and got and got != want:
                return None
        return rows[0]
    if not want_flat:
        return None                       # дом без квартиры — что именно проверять, неясно

    def _same(row: dict[str, Any]) -> bool:
        addr = str(row.get("address") or "")
        if _flat_of(addr) != want_flat:
            return False
        if want_house and _house_of(addr) != want_house:
            return False
        # Корпус сверяем, только если он назван в обеих строках: в выдаче
        # встречаются адреса без корпуса, и требовать его жёстко значит терять
        # верные попадания.
        if want_bldg and _bldg_of(addr) and _bldg_of(addr) != want_bldg:
            return False
        return True

    hits = [r for r in rows if _same(r)]
    return hits[0] if len(hits) == 1 else None


# ── перевод в форму NewDB, чтобы разбор в rosreestr.py не менялся ───────────
# api-assist отдаёт ТЕКСТ обременения, NewDB — код. Восстанавливаем код, иначе
# _ENC_SERIOUS не сработает и арест уедет в отчёт как рядовое ограничение.
_TEXT_TO_CODE: list[tuple[str, str]] = [
    ("запрещение", "022017"), ("запрет", "022017"),
    ("арест", "022016"),
    ("изъят", "022018"),
    ("ипотека в силу закона", "022002"), ("ипотека", "022001"),
    ("аренда", "022006"),
    ("безвозмездн", "022007"),
    ("довер", "022010"),
    ("рента", "022012"), ("пожизненн", "022012"),
    ("сервитут", "022013"),
]


def _enc_code_from_text(text: str) -> str:
    low = (text or "").strip().lower()
    for needle, code in _TEXT_TO_CODE:
        if needle in low:
            return code
    return "022099"   # иное ограничение — но НЕ пусто: обременение есть


def to_newdb_shape(rec: dict[str, Any]) -> dict[str, Any]:
    """Карточка api-assist → структура, которую уже умеет читать rosreestr.py."""
    enc_out: list[dict[str, Any]] = []
    for e in rec.get("encumbrances") or []:
        if not isinstance(e, dict):
            continue
        desc = str(e.get("type") or "").strip()
        enc_out.append({
            "type": _enc_code_from_text(desc) + "000000",
            "encumbranceTypeDesc": desc or "обременение",
            "startDate": e.get("date") or "",
            "regNumber": e.get("number") or "",
        })

    rights_in = [r for r in (rec.get("rights") or []) if isinstance(r, dict)]
    shared = any("долев" in str(r.get("type") or "").lower() for r in rights_in)
    rights_out: list[dict[str, Any]] = []
    for r in rights_in:
        rights_out.append({
            # part оставляем пустым — доли api-assist не отдаёт, а выдумывать
            # их нельзя. Долевую собственность вызывающий код распознаёт
            # по rightTypeDesc (см. check_object).
            "part": "",
            "rightNumber": r.get("number") or "",
            "rightTypeDesc": r.get("type") or "",
            "regDate": r.get("date") or "",
        })

    return {
        "cadNumber": rec.get("cad_number") or "",
        "area": rec.get("area"),
        "purpose_text": rec.get("purpose") or rec.get("type") or "",
        "objType_text": rec.get("type") or "",
        "address": rec.get("address") or "",
        "cost": rec.get("cad_cost"),
        "floor": rec.get("floor"),
        "status": rec.get("status"),
        "ownership": rec.get("ownership"),
        "encumbrances": enc_out,
        "rights": rights_out,
        "_source": "api-assist",
    }


# ── банкротство физлица по ФИО (Федресурс) ──────────────────────────────────
def search_bankruptcy(last: str, first: str, patronymic: str = "",
                      inn: str = "", timeout: float = 90.0) -> list[dict[str, Any]]:
    """Записи о банкротстве физлица. ПОИСК ИДЁТ ПО ФИО — ИНН не обязателен.

    Это снимает главный барьер апселла: у NewDB метод bankrot_person принимает
    ТОЛЬКО innfiz, поэтому мы требовали у покупателя квартиры ИНН продавца —
    данные, которых у него нет. Ноль продаж апселла за всё время тому и причина.

    ИНН здесь наоборот ПРИХОДИТ в ответе (вместе со СНИЛС, регионом и адресом)
    и служит для развязки однофамильцев. Пустой список = банкротств не найдено.
    """
    params: dict[str, str] = {"lastName": last.strip(), "firstName": first.strip()}
    if patronymic.strip():
        params["patronymic"] = patronymic.strip()
    code = re.sub(r"\D", "", inn or "")
    if len(code) in (10, 12):
        params["fizCode"] = code      # если ИНН всё-таки известен — сужаем сразу
    data = _get("search_fiz", params, timeout, base=FEDRESURS)
    rows = data.get("records")
    if rows is None:
        raise ApiAssistError("нет поля records")
    return [r for r in rows if isinstance(r, dict)]


def narrow_by_region(rows: list[dict[str, Any]], place_hint: str) -> list[dict[str, Any]]:
    """Отсеять однофамильцев по месту. place_hint — адрес объекта в свободной форме.

    Федресурс отдаёт НАЗВАНИЕ региона («Калужская область»), а у нас код («40»),
    поэтому сравниваем с адресом объекта, который и так есть в превью:
    «Калужская обл., г. Калуга, ул. Дружбы» → корень «калужс» совпадает.

    Если после фильтра не осталось никого — возвращаем всех. Пустой результат
    здесь означал бы «банкротств нет», а это была бы ложь: они есть, просто
    в другом регионе, и решать должен человек.
    """
    hint = (place_hint or "").lower()
    if not hint or len(rows) < 2:
        return rows
    hits = [r for r in rows if _region_root(str(r.get("region") or "")) and
            _region_root(str(r.get("region") or "")) in hint]
    return hits or rows


# Служебные слова в названиях регионов. Без их отсева «г. Санкт-Петербург»
# давал корень «г» — одна буква, которая есть в любой подсказке, и фильтр
# пропускал кого угодно (поймано на проде: подсказка «Калужская обл.»
# оставляла в кандидатах петербуржца).
_REGION_STOP = {"г", "гор", "обл", "область", "респ", "республика", "край",
                "ао", "автономный", "автономная", "округ", "район", "р-н"}


def _region_root(region: str) -> str:
    """Опознаваемый корень названия региона: «Калужская область» → «калужс»."""
    for tok in re.split(r"[\s.,\-]+", (region or "").strip().lower()):
        if len(tok) >= 4 and tok not in _REGION_STOP:
            return tok[:6]
    return ""


# Район в адресе. Люди пишут его так, как называется посёлок («Серебряные Пруды
# р-н»), а в реестре он лежит прилагательным («Серебряно-Прудский р-н»), и поиск
# сравнивает строку буквально: одна неверная форма обнуляет всю выдачу целиком.
_DISTRICT_WORD = r"(?:р-?н|район)"
# Район в конце поля: «… Серебряные Пруды р-н». Имя — до трёх слов, и слово с
# точкой (сокращение региона «М.О.») в имя не берём: иначе вырежем регион.
_DISTRICT_TAIL = re.compile(
    r"(?:(?<=\s)|^)(?:[^\s,.]+\s+){0,3}" + _DISTRICT_WORD + r"\.?\s*$", re.I)
# Район в начале поля: «р-н. Серебряно-Прудский …».
_DISTRICT_HEAD = re.compile(
    r"^\s*" + _DISTRICT_WORD + r"\.?\s*(?:[^\s,.]+\s*){0,3}$", re.I)


def _without_district(address: str) -> str:
    """Тот же адрес без района. Пусто — если района не было (повторять нечего).

    Режем по полям между запятыми: район может стоять как отдельным полем, так и
    слипшимся с регионом («М.О. СЕРЕБРЯНЫЕ ПРУДЫ Р-Н.») — во втором случае
    выкидываем только район, регион обязан остаться, иначе поиск уйдёт по всей
    стране и найдёт однофамильную улицу в другой области.
    """
    src = (address or "").strip(" ,")
    if not src:
        return ""
    out, cut = [], False
    for seg in src.split(","):
        s = seg.strip()
        if not s:
            continue
        if not cut and re.search(_DISTRICT_WORD + r"\b\.?", s, re.I):
            rest = _DISTRICT_HEAD.sub("", s)
            if rest == s:
                rest = _DISTRICT_TAIL.sub("", s)
            if rest != s:                      # район опознан и вырезан
                cut = True
                if rest.strip(" .,"):
                    out.append(rest.strip(" ,"))
                continue
        out.append(s)
    res = ", ".join(out)
    return res if cut and res else ""


def object_rows(object_ref: str, timeout: float = 90.0) -> list[dict[str, Any]]:
    """Главная точка входа: кадастр или адрес → строки в форме NewDB.

    Пустой список = источник ОТВЕТИЛ «не найдено» (или выбрать объект из
    выдачи нельзя). Исключение = источник не ответил."""
    ref = (object_ref or "").strip()
    if not ref:
        return []
    if re.match(r"^\s*\d{2}:\d{1,2}:\d{1,7}:\d{1,7}\s*$", ref):
        rows = details_by_cadastre(ref, timeout)
    else:
        found = search_by_address(ref, timeout)
        # Пусто — пробуем без района. Отчёт 157a0baf74d1: клиентка написала
        # «М.О. СЕРЕБРЯНЫЕ ПРУДЫ Р-Н.», реестр вернул НОЛЬ строк, объект числился
        # несуществующим и заказ завис оплаченным. Тот же адрес без района даёт
        # ровно одну строку — нужную. Регион, посёлок, улица, дом и квартира при
        # этом остаются, а pick_object всё равно сверяет дом, корпус и помещение,
        # так что чужой объект через это не пролезет.
        if not found:
            short = _without_district(ref)
            if short:
                found = search_by_address(short, timeout)
                if found:
                    print(f"[addr] нашли без района: {ref[:60]}", flush=True)
        chosen = pick_object(found, ref)
        if not chosen:
            return []
        cad = str(chosen.get("cad_number") or "")
        rows = details_by_cadastre(cad, timeout) if cad else []
    return [to_newdb_shape(r) for r in rows]
