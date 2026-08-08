"""Бесплатные данные объекта из НСПД (nspd.gov.ru) — публичный портал Росреестра.

Даёт по кадастровому номеру БЕСПЛАТНО (не через платный NewDB): тип, площадь,
назначение, адрес, кадастровую стоимость, этаж, статус, форму собственности.
Используется в ПРЕВЬЮ, чтобы показать реальный вес объекта до оплаты, не тратя деньги.
Собственники/обременения/аресты НСПД не отдаёт — они остаются на платную полную проверку.

Гоча: у Росреестра российский ГОСТ-сертификат (нет в стандартном trust store) → verify=False.
По свободному адресу НСПД не ищет — только по кадастру (адрес → объект после оплаты).
"""
from __future__ import annotations

import os
import re
from typing import Any

import httpx

NSPD_URL = "https://nspd.gov.ru/api/geoportal/v1/search/geoportal"
DADATA_CLEAN_URL = "https://cleaner.dadata.ru/api/v1/clean/address"
_KAD_RE = re.compile(r"^\s*\d{2}:\d{1,2}:\d{1,7}:\d{1,7}\s*$")

# кэш адрес→кадастр, чтобы не платить DaData повторно за тот же адрес (в т.ч. боты).
# Ключ НОРМАЛИЗОВАН (lower + схлопнутые пробелы). Кэшируем ТОЛЬКО успешные ответы —
# иначе транзиентный сбой (таймаут/5xx) навсегда отравил бы адрес значением None.
_addr_cache: dict[str, str | None] = {}
# Разобранный адрес кэшируем целиком: один ответ DaData обслуживает и поиск кадастра,
# и вывод «мы вас поняли» при неудаче — платить за него дважды незачем.
_clean_cache: dict[str, dict] = {}


def _norm_addr(a: str) -> str:
    return " ".join((a or "").lower().split())


# Номер квартиры во вводе. Нужен, чтобы отличить «человек покупает помещение» от
# «человек покупает дом»: в первом случае кадастр ЗДАНИЯ показывать как найденный
# объект нельзя — он не то, за что платят.
_FLAT_RE = re.compile(r"(?:^|[\s,.;])(?:кв|квартира|пом|помещение|офис)[\s.]*№?\s*(\d+[а-яa-z]?)", re.I)


def flat_in_address(address: str) -> str:
    """Номер квартиры из свободного ввода. Пусто — если не указан."""
    m = _FLAT_RE.search(address or "")
    return m.group(1) if m else ""


def clean_address(address: str, timeout: float = 12.0) -> dict[str, Any]:
    """Разбор адреса через DaData Clean (~0.2₽, первые 100 бесплатно). Один запрос —
    и кадастр квартиры, и кадастр дома, и разобранные поля.

    Раньше отсюда возвращалась одна строка `flat_cadnum or house_cadnum`, и подмена
    молча ломала главное: не сумев определить квартиру, DaData отдавала номер ДОМА,
    а превью показывало человеку «Здание 6 213 м²» как его объект. Он платил за то,
    что мы проверить не могли. Теперь номера разведены, и решает вызывающий код.

    Разобранные поля нужны и сами по себе: когда объект не нашёлся, показать «регион,
    район, посёлок, улица, индекс» — единственный способ подтвердить человеку, что мы
    его поняли. Стоит это ноль: поля приходят тем же ответом.
    """
    empty: dict[str, Any] = {"flat_cad": "", "house_cad": "", "clean": "", "region_code": "",
                             "parts": [], "ok": False}
    addr = (address or "").strip()
    if not addr or is_kadastr(addr):
        return empty
    ck = _norm_addr(addr)
    if ck in _clean_cache:
        return _clean_cache[ck]
    key = os.getenv("DADATA_API_KEY", "").strip()
    secret = os.getenv("DADATA_SECRET_KEY", "").strip()
    if not (key and secret):
        return empty
    out = dict(empty)
    try:
        r = httpx.post(
            DADATA_CLEAN_URL, json=[addr],
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": f"Token {key}", "X-Secret": secret},
            timeout=timeout,
        )
        if r.status_code == 200:
            o = (r.json() or [{}])[0] or {}
            out["ok"] = True            # ответ получен (даже пустой) — кэшируем
            out["flat_cad"] = (o.get("flat_cadnum") or "").strip()
            out["house_cad"] = (o.get("house_cadnum") or "").strip()
            out["clean"] = (o.get("result") or "").strip()
            out["region_code"] = (o.get("region_kladr_id") or "")[:2]
            # Порядок полей — от крупного к мелкому: человек читает сверху вниз и
            # узнаёт свой адрес по первым же строкам.
            for label, val in (("Регион", o.get("region_with_type")),
                               ("Район", o.get("area_with_type")),
                               ("Город", o.get("city_with_type")),
                               ("Населённый пункт", o.get("settlement_with_type")),
                               ("Улица", o.get("street_with_type")),
                               ("Дом", o.get("house")),
                               ("Квартира", o.get("flat")),
                               ("Индекс", o.get("postal_code"))):
                if val:
                    out["parts"].append([label, str(val)])
    except Exception:  # noqa: BLE001 — транзиентный сбой не кэшируем, дадим ретрай
        return empty
    if out["ok"]:
        if len(_clean_cache) > 20000:
            _clean_cache.clear()
        _clean_cache[ck] = out
    return out


def cadastre_by_address(address: str, timeout: float = 12.0,
                        allow_house: bool = True) -> str | None:
    """Адрес → кадастровый номер. allow_house=False запрещает подставлять номер ДОМА
    вместо квартиры — так и надо, когда во вводе номер квартиры указан."""
    addr = (address or "").strip()
    if not addr:
        return None
    if is_kadastr(addr):
        return addr
    d = clean_address(addr, timeout=timeout)
    return d.get("flat_cad") or (d.get("house_cad") if allow_house else "") or None


def is_kadastr(s: str) -> bool:
    return bool(_KAD_RE.match(s or ""))


def _object_via_apiassist(cad: str) -> dict[str, Any] | None:
    """Те же характеристики, но из api-assist. Запасной путь: НСПД периодически
    недоступен (сегодня отбил WAF-ом), а с не-российских адресов не отвечает вовсе —
    без этого превью на локальной машине не собрать. По умолчанию ВЫКЛЮЧЕН: тратит
    суточную квоту, а на проде НСПД работает и бесплатен."""
    if os.getenv("NSPD_FALLBACK_APIASSIST", "").strip() not in ("1", "true", "yes"):
        return None
    try:
        from modules import apiassist
        rows = apiassist.details_by_cadastre(cad, timeout=60)
    except Exception:  # noqa: BLE001 — запасной путь не имеет права ломать основной
        return None
    if not rows:
        return None
    o = rows[0] or {}
    def _num(v):
        try:
            return float(str(v).replace(",", "."))
        except Exception:  # noqa: BLE001
            return None
    return {"cad": o.get("cad_number") or cad, "type": o.get("type"),
            "area": _num(o.get("area")), "purpose": o.get("purpose"),
            "address": o.get("address"), "cost": _num(o.get("cad_cost")),
            "floor": o.get("floor"), "status": o.get("status"),
            "ownership": o.get("ownership"), "reg_date": o.get("reg_date"),
            "category": None, "is_land": "участок" in str(o.get("type") or "").lower(),
            "land_category": o.get("land_category"), "right_type": None}


def object_by_cadastre(cad: str, timeout: float = 15.0) -> dict[str, Any] | None:
    """Характеристики объекта по кадастру: сначала бесплатный НСПД, затем — если он
    молчит и включён флаг — платный запасной источник.

    В ответ кладём поле `source`: без него в статистике не отличить бесплатный НСПД
    от платного запасного, и нельзя ответить, сколько объектов api-assist вытащил
    из тех, что НСПД не отдал.
    """
    o = _object_via_nspd(cad, timeout)
    if o:
        o["source"] = "nspd"
        return o
    o = _object_via_apiassist((cad or "").strip())
    if o:
        o["source"] = "apiassist"
    return o


def _object_via_nspd(cad: str, timeout: float = 15.0) -> dict[str, Any] | None:
    """Характеристики объекта из НСПД по кадастру. None — если не кадастр/не найден/сбой."""
    cad = (cad or "").strip()
    if not is_kadastr(cad):
        return None
    try:
        r = httpx.get(
            NSPD_URL, params={"query": cad},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "Referer": "https://nspd.gov.ru/"},
            timeout=timeout, verify=False, follow_redirects=True,
        )
        if r.status_code != 200:
            return None
        feats = (r.json() or {}).get("features") or []
        if not feats:
            return None
        props = feats[0].get("properties") or {}
        o = props.get("options") or {}
        floor = o.get("floor") or []
        category = str(props.get("categoryName") or "")

        # У РАЗНЫХ категорий НСПД поля называются по-разному, и чтение только
        # «квартирных» имён давало пустую карточку: 225 заявок за 4 дня (27% всех
        # превью с объектом), из них 95% — земельные участки. Конверсия таких
        # превью 1,8% против 4,3% у нормальных: человек видит подтверждение
        # без единой характеристики и уходит.
        is_land = "Земельны" in category or o.get("land_record_type")
        kind = (o.get("params_type") or o.get("type")
                or o.get("land_record_type")            # «Земельный участок»
                or ("Здание" if "Здания" in category else None)
                or ("Сооружение" if "Сооружен" in category else None))
        area = (o.get("area") or o.get("land_record_area")
                or o.get("specified_area"))             # у участка площадь здесь
        purpose = (o.get("purpose")
                   or o.get("permitted_use_established_by_document"))  # ВРИ участка

        return {
            "cad": o.get("cad_number") or o.get("cad_num") or cad,
            "type": kind,                                            # Квартира / Земельный участок
            "area": area,                                            # 31.1 или 1223
            "purpose": purpose,                     # Жилое / «для садоводства»
            "address": o.get("readable_address"),
            "cost": o.get("cost_value"),
            "floor": (str(floor[0]).split("/")[0] if floor else None),
            "status": o.get("common_data_status") or o.get("status"),
            "ownership": o.get("ownership_type"),
            "reg_date": ((o.get("registration_date") or o.get("land_record_reg_date") or "")[:10]),
            # новое: категория объекта и категория земель — чтобы карточка
            # называла вещи своими именами, а не молчала
            "category": category,
            "is_land": bool(is_land),
            "land_category": o.get("land_record_category_type"),     # Земли населённых пунктов
            "right_type": o.get("right_type"),                       # Собственность
        }
    except Exception:  # noqa: BLE001 — best effort, при сбое превью просто без объекта
        return None
