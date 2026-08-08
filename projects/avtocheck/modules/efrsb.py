"""
ЕФРСБ / Федресурс — проверка банкротства физлица по ИНН через NewDB.

NewDB метод bankrot_person принимает ИНН (innfiz) и возвращает записи о
банкротстве через Федресурс. ИНН — опциональное поле формы: если пользователь
его указал, проверяем банкротство вживую; иначе честно NOT_CHECKED.

Никогда не выдаём молчание источника за "чисто".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from modules import newdb


@dataclass
class EFRSBResult:
    status: str  # "found" | "not_found" | "not_checked"
    detail: str
    items: list[dict[str, Any]]
    source_url: str = "https://bankrot.fedresurs.ru/"


def _clean_inn(inn: str) -> str:
    digits = re.sub(r"\D", "", inn or "")
    return digits if len(digits) in (10, 12) else ""


def check_bankruptcy(
    full_name: str, dob: str, inn: str = "", timeout: float = 300.0,
    place_hint: str = "",
) -> EFRSBResult:
    """Банкротство физлица. ИНН НЕ обязателен — без него ищем по ФИО.

    place_hint — адрес объекта в свободной форме, нужен только для развязки
    однофамильцев при поиске по ФИО."""
    clean_inn = _clean_inn(inn)
    if not clean_inn:
        # Раньше здесь был тупик: «укажите ИНН продавца». Покупатель квартиры
        # ИНН продавца не знает и знать не должен — отсюда ноль продаж апселла.
        # На деле ИНН не нужен для ПОИСКА, он приходит В ОТВЕТЕ Федресурса.
        return _by_name(full_name, place_hint, timeout)

    if not newdb.token():
        return EFRSBResult(
            status="not_checked",
            detail="Банкротство: проверка временно недоступна (не настроен доступ).",
            items=[],
        )

    # Через общий newdb.call, а не своей копией транспорта: копия не размыкала
    # поллинг на state="failed" (досиживала дедлайн по мёртвому запросу) и не
    # попадала в журнал списаний newdb_calls.jsonl — расходы были невидимы.
    try:
        rows = newdb.call("bankrot_person", {"country": "ru", "innfiz": clean_inn},
                          timeout=timeout, attempts=1)
    except newdb.NewDBError as e:
        return EFRSBResult(
            status="not_checked",
            detail=f"Банкротство: сервис недоступен ({e}).",
            items=[],
        )
    return _summarize(rows)


def _split_fio(full_name: str) -> tuple[str, str, str]:
    parts = [p for p in re.split(r"\s+", (full_name or "").strip()) if p]
    while len(parts) < 3:
        parts.append("")
    return parts[0], parts[1], parts[2]


def _by_name(full_name: str, place_hint: str, timeout: float) -> EFRSBResult:
    """Поиск банкротства по ФИО через Федресурс (api-assist), без ИНН."""
    last, first, patr = _split_fio(full_name)
    if not last or not first:
        return EFRSBResult(
            status="not_checked",
            detail="Банкротство: недостаточно данных (нужны фамилия и имя).",
            items=[],
        )
    try:
        from modules import apiassist
        if not apiassist.enabled():
            return EFRSBResult(
                status="not_checked",
                detail="Банкротство: проверка временно недоступна (не настроен доступ).",
                items=[],
            )
        rows = apiassist.search_bankruptcy(last, first, patr, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return EFRSBResult(
            status="not_checked",
            detail=f"Банкротство: сервис недоступен ({type(e).__name__}).",
            items=[],
        )

    if not rows:
        return EFRSBResult(
            status="not_found",
            detail="Банкротство: записей о банкротстве продавца не найдено.",
            items=[],
        )

    from modules import apiassist as _aa
    narrowed = _aa.narrow_by_region(rows, place_hint)
    items = [{"name": r.get("debtor"), "inn": r.get("inn"),
              "region": r.get("region"), "address": r.get("address")}
             for r in narrowed[:10]]

    if len(narrowed) > 1:
        # НЕ выбираем наугад. Назвать банкротом не того человека хуже, чем
        # не проверить: покупатель сорвёт сделку, а продавец придёт с иском.
        return EFRSBResult(
            status="not_checked",
            detail=(
                f"Банкротство: по ФИО найдено несколько человек ({len(narrowed)}) — "
                "однофамильцы. Точно определить продавца можно по его ИНН: "
                "укажите его, и мы уточним проверку."
            ),
            items=items,
        )
    return EFRSBResult(
        status="found",
        detail=(f"Банкротство: найдена запись в реестре — {narrowed[0].get('debtor')}, "
                f"{narrowed[0].get('region') or 'регион не указан'}. "
                "Сделка с банкротом может быть оспорена управляющим."),
        items=items,
    )


def _summarize(rows: list[dict[str, Any]]) -> EFRSBResult:
    bankruptcies: list[dict[str, Any]] = []
    person_name = ""
    for row in rows:
        common = row.get("commmon") or row.get("common") or {}
        person_name = person_name or common.get("name_or_fio", "")
        for b in row.get("bankruptcy") or []:
            if isinstance(b, dict):
                bankruptcies.append(
                    {
                        "name": common.get("name_or_fio", person_name),
                        "inn": common.get("inn", ""),
                        "stage": b.get("stage") or b.get("status") or b.get("type") or "",
                        "case": b.get("case_number") or b.get("number") or "",
                        "details_url": common.get("details_url", ""),
                    }
                )
    if bankruptcies:
        return EFRSBResult(
            status="found",
            detail=(
                f"⚠️ В реестре банкротств найдены записи ({len(bankruptcies)}). "
                "Это высокий риск оспаривания сделки (ст. 61.2 №127-ФЗ)."
            ),
            items=bankruptcies[:10],
        )
    return EFRSBResult(
        status="not_found",
        detail="В реестре банкротств (Федресурс) записей не найдено.",
        items=[],
    )
