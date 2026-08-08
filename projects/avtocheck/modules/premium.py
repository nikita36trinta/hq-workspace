"""
Премиум-проверки физлица через NewDB: паспорт+ИНН, арбитраж, залоги.

Используются в тарифе «Полная проверка»: пользователь вводит паспорт продавца
→ passport_fns даёт действительность паспорта + ИНН → по ИНН проверяем
банкротство и арбитраж, по ФИО — залоги.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from modules import newdb


@dataclass
class CheckOut:
    status: str  # "found" | "not_found" | "not_checked"
    detail: str
    items: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # напр. {"inn": "..."}


def _dob_iso(dob: str) -> str:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(dob.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _fio(full_name: str) -> tuple[str, str, str]:
    p = [x for x in full_name.strip().split() if x]
    return (p[0] if p else "", p[1] if len(p) > 1 else "", " ".join(p[2:]) if len(p) > 2 else "")


def passport_and_inn(full_name: str, dob: str, seria: str, nomer: str) -> CheckOut:
    """Получение ИНН продавца по паспорту через ФНС (passport_fns).

    Метод сопоставляет паспорт с реестром ФНС и возвращает ИНН (поле innfiz).
    Отдельного поля «действительность» у метода нет: наличие ИНН = паспорт
    успешно сопоставлен с данными ФНС; пустой ответ = совпадение не найдено
    (неверные данные ИЛИ паспорт не числится в реестре).
    """
    last, first, patr = _fio(full_name)
    dob_iso = _dob_iso(dob)
    seria = re.sub(r"\D", "", seria or "")
    nomer = re.sub(r"\D", "", nomer or "")
    if not (last and first and dob_iso and len(seria) == 4 and len(nomer) == 6):
        return CheckOut("not_checked", "Паспорт: укажите серию (4 цифры) и номер (6 цифр).")
    try:
        rows = newdb.call("passport_fns", {
            "lastname": last, "firstname": first, "secondname": patr,
            "dob": dob_iso, "seria": seria, "number": nomer,
        })
    except newdb.NewDBError as e:
        return CheckOut("not_checked", f"Паспорт/ИНН: сервис недоступен ({e}).")
    if not rows:
        return CheckOut(
            "not_checked",
            "Паспорт: ИНН по данным паспорта в реестре ФНС не найден — "
            "проверьте корректность серии/номера/ФИО/даты рождения.",
            extra={"inn": ""},
        )
    row = rows[0] if isinstance(rows[0], dict) else {}
    inn = str(row.get("innfiz") or row.get("inn") or "")
    if inn:
        return CheckOut(
            "not_found",
            f"Паспорт сопоставлен с реестром ФНС, ИНН получен ({inn[:4]}…). "
            "По ИНН выполнены проверки банкротства и арбитража.",
            extra={"inn": inn},
        )
    return CheckOut(
        "not_checked",
        "Паспорт: ответ ФНС без ИНН — проверьте введённые данные.",
        extra={"inn": ""},
    )


def arbitration(inn: str, timeout: float = 150.0) -> CheckOut:
    """Арбитражные дела физлица/ИП по ИНН (arbitr_person).

    Самый медленный метод — запускается ОТДЕЛЬНОЙ волной (когда токен NewDB
    свободен от других проверок) с увеличенным лимитом ожидания.
    """
    inn = re.sub(r"\D", "", inn or "")
    if len(inn) not in (10, 12):
        return CheckOut("not_checked", "Арбитраж: нужен ИНН (укажите паспорт для его получения).")
    try:
        rows = newdb.call("arbitr_person", {"innfiz": inn}, timeout=timeout)
    except newdb.NewDBError as e:
        return CheckOut("not_checked", f"Арбитраж: сервис недоступен ({e}).")
    cases: list[dict[str, Any]] = []
    for r in rows:
        # r может оказаться не словарём: провайдер иногда отдаёт список строк.
        # Раньше .get() на такой строке бросал AttributeError МИМО except выше —
        # уже после оплаченного запроса. Именно это исключение раскручивало
        # burn-loop мониторинга (next_run не сдвигался, прогон повторялся).
        if not isinstance(r, dict):
            continue
        for c in (r.get("cases") or r.get("arbitr") or ([r] if r.get("case_number") else [])):
            if isinstance(c, dict):
                cases.append({
                    "case": c.get("case_number") or c.get("number") or "",
                    "role": c.get("role") or c.get("type") or "",
                    "court": c.get("court") or "",
                    "date": c.get("date") or "",
                })
    if cases:
        return CheckOut("found", f"Найдены арбитражные дела: {len(cases)}.", cases[:10])
    return CheckOut("not_found", "Арбитражных дел не найдено.")


def pledges(full_name: str) -> CheckOut:
    """Залоги/обременения движимого имущества по ФИО (pledge_person)."""
    last, first, patr = _fio(full_name)
    if not last or not first:
        return CheckOut("not_checked", "Залоги: недостаточно данных (ФИО).")
    # Без отчества источник отвечает «secondname must be non-empty» ещё до поиска,
    # а мы показывали это как «сервис недоступен» — будто сломались мы. Говорим
    # прямо, чего не хватает; платного запроса тут всё равно не будет.
    if not patr:
        return CheckOut("not_checked",
                        "Залоги: нужно отчество продавца — реестр ищет по полному ФИО.")
    try:
        rows = newdb.call("pledge_person", {"lastname": last, "firstname": first, "secondname": patr})
    except newdb.NewDBError as e:
        return CheckOut("not_checked", f"Залоги: сервис недоступен ({e}).")
    plist: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):   # см. комментарий в arbitration()
            continue
        for p in (r.get("pledges") or ([r] if r.get("pledge_number") or r.get("number") else [])):
            if isinstance(p, dict):
                plist.append({
                    "number": p.get("pledge_number") or p.get("number") or "",
                    "subject": p.get("subject") or p.get("property") or "",
                    "date": p.get("date") or "",
                })
    if plist:
        return CheckOut("found", f"Найдены залоги/обременения: {len(plist)}.", plist[:10])
    return CheckOut("not_found", "Залогов и обременений по физлицу не найдено.")
