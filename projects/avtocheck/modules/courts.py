"""
Суды общей юрисдикции (ГАС «Правосудие») и КАД Арбитр.

Открытого стабильного API для поиска физлица по ФИО ни у ГАС «Правосудие»,
ни у КАД Арбитр нет: sudrf.ru отдаёт HTML+капча, kad.arbitr.ru требует
POST с CSRF и часто уходит в 403. Поэтому честно помечаем "не проверено"
со ссылкой на источники, а не выдумываем "чисто".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CourtsResult:
    status: str  # "found" | "not_found" | "not_checked"
    detail: str
    items: list[dict[str, Any]]
    source_url: str = "https://sudrf.ru/"


def check_courts(full_name: str, dob: str) -> CourtsResult:
    return CourtsResult(
        status="not_checked",
        detail=(
            "ГАС «Правосудие» / КАД Арбитр: открытого API поиска по ФИО нет "
            "(портал защищён капчей). Проверьте вручную на sudrf.ru и kad.arbitr.ru."
        ),
        items=[],
    )
