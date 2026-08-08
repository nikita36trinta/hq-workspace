"""
Агрегатор проверок → уровень риска + рекомендации.

Правила (детерминированные, работают без LLM):
- банкротство найдено         → ВЫСОКИЙ (ст. 61.2 ФЗ-127)
- крупные исп. производства   → СРЕДНИЙ (ФЗ-229)
- любой источник "не проверено" И нет флагов → СРЕДНИЙ (нельзя выдать "низкий")
- всё проверено и всё чисто    → НИЗКИЙ

LLM (опционально, при наличии ANTHROPIC_API_KEY) переписывает вердикт в
человеческом тоне, но НЕ меняет уровень риска — уровень всегда из правил.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any


RISK_LOW = "низкий"
RISK_MEDIUM = "средний"
RISK_HIGH = "высокий"


@dataclass
class Check:
    key: str
    name: str
    source: str
    status: str  # "found" | "not_found" | "not_checked"
    detail: str
    items: list[dict[str, Any]] = field(default_factory=list)
    # not_checked ПОСТОЯННО (объекта нет в ЕГРН) — ретрай бессмыслен и платен;
    # False = временный сбой источника, повтор оправдан.
    permanent: bool = False


@dataclass
class Verdict:
    risk: str
    headline: str
    body: str
    recommendations: list[str]
    llm_used: bool = False


def _has_bankruptcy(checks: list[Check]) -> bool:
    return any(c.key == "bankruptcy" and c.status == "found" for c in checks)


def _has_enforcement(checks: list[Check]) -> bool:
    return any(c.key == "enforcement" and c.status == "found" for c in checks)


def _object_flagged(checks: list[Check]) -> bool:
    """У объекта есть пометки ЕГРН (обременения и/или долевая) — status='found'."""
    return any(c.key == "object" and c.status == "found" for c in checks)


def _object_serious(checks: list[Check]) -> bool:
    """Серьёзное обременение объекта — арест/запрет/изъятие → высокий риск."""
    for c in checks:
        if c.key == "object" and c.status == "found":
            t = (c.detail or "").lower()
            if "арест" in t or "запрещ" in t or "запрет" in t or "изъят" in t:
                return True
    return False


def _has_arbitration(checks: list[Check]) -> bool:
    """Арбитражные дела против продавца (в т.ч. заявления о банкротстве) — status='found'."""
    return any(c.key == "arbitration" and c.status == "found" for c in checks)


def _has_pledges(checks: list[Check]) -> bool:
    """Залоги/обременения движимого имущества продавца — status='found'."""
    return any(c.key == "pledges" and c.status == "found" for c in checks)


def _any_not_checked(checks: list[Check]) -> bool:
    return any(c.status == "not_checked" for c in checks)


def _all_clean(checks: list[Check]) -> bool:
    return all(c.status == "not_found" for c in checks)


# Блоки авто-отчёта, при находке в которых сделку надо ОСТАНАВЛИВАТЬ, а не
# торговаться. Залог: по ст. 353 ГК переходит вместе с машиной к покупателю.
# Ограничения и розыск: автомобиль просто не переоформят на нового владельца.
_AUTO_STOP = {"pledge", "restrict", "wanted"}
# Блоки, которые влияют на цену и требуют внимательного осмотра, но сделку не
# запрещают: битая, много владельцев, работала в найме, ввезена из-за границы.
_AUTO_WARN = {"dtp", "history", "taxi", "customs", "mileage"}


def compute_risk(checks: list[Check]) -> str:
    """Риск покупки автомобиля.

    Логика отличается от недвижимости принципиально: там главное — банкротство
    продавца и оспаривание сделки, здесь — залог и запрет регистрации, из-за
    которых машину либо заберут, либо не поставят на учёт.
    """
    by_key = {c.key: c for c in checks}

    # ВЫСОКИЙ: нашлось то, из-за чего покупать нельзя вообще.
    if any(by_key.get(k) is not None and by_key[k].status == "found" for k in _AUTO_STOP):
        return RISK_HIGH

    found_warn = [k for k in _AUTO_WARN
                  if by_key.get(k) is not None and by_key[k].status == "found"]

    # СРЕДНИЙ: есть находки по «ценовым» блокам либо ключевой блок не проверен.
    if found_warn:
        return RISK_MEDIUM
    # Не проверился хотя бы один СТОП-блок — молчание источника не «чисто».
    if any(by_key.get(k) is not None and by_key[k].status == "not_checked"
           for k in _AUTO_STOP):
        return RISK_MEDIUM

    if _all_clean(checks):
        return RISK_LOW
    if _any_not_checked(checks):
        return RISK_MEDIUM
    return RISK_LOW


def _rule_recommendations(checks: list[Check], risk: str) -> list[str]:
    recs: list[str] = []
    if _has_bankruptcy(checks):
        recs.append(
            "В ЕФРСБ найдено дело о банкротстве продавца — сделка попадает под риск "
            "оспаривания по ст. 61.2 ФЗ-127. Не выходите на сделку без консультации "
            "юриста по банкротству."
        )
    if _has_enforcement(checks):
        recs.append(
            "У продавца есть исполнительные производства — попросите справку об "
            "отсутствии задолженности от приставов и проверьте, нет ли запретов "
            "регистрационных действий в ЕГРН."
        )
    if _object_flagged(checks):
        recs.append(
            "В ЕГРН у объекта есть обременения/ограничения — запросите свежую выписку "
            "ЕГРН с их расшифровкой (какие именно, в чью пользу, на каком основании) "
            "и снимите до подписания договора. Аванс до снятия обременений не вносите."
        )
    if _has_arbitration(checks):
        recs.append(
            "Против продавца найдены арбитражные дела — уточните их суть (особенно "
            "заявления о банкротстве и крупные взыскания): они могут привести к "
            "оспариванию сделки. Проверьте статус дел в КАД (kad.arbitr.ru) до сделки."
        )
    if _has_pledges(checks):
        recs.append(
            "У продавца найдены залоги/обременения имущества — убедитесь, что "
            "приобретаемый объект не в залоге, и запросите документы об основаниях "
            "залога. Средства в залоге у третьих лиц повышают риск оспаривания."
        )
    if _any_not_checked(checks):
        not_checked_names = [c.name for c in checks if c.status == "not_checked"]
        recs.append(
            "Часть источников не удалось проверить автоматически: "
            + ", ".join(not_checked_names)
            + ". До сделки перепроверьте вручную на официальных порталах."
        )
    if risk == RISK_LOW:
        recs.append(
            "Запросите у продавца свежую выписку ЕГРН (не старше 2 недель) "
            "и справку о зарегистрированных лицах перед авансом."
        )
    if not recs:
        recs.append(
            "Рекомендуем стандартный набор: свежая выписка ЕГРН, нотариальное "
            "согласие супруга (если применимо), справка об отсутствии "
            "зарегистрированных лиц."
        )
    return recs


# «право зарегистрировано ДД.ММ.ГГГГ» — эту строку кладёт в деталь блока ЕГРН
# modules/rosreestr.py. Дата нужна и заголовку, и промпту.
_REG_DATE_RE = re.compile(r"право зарегистрировано (\d{2})\.(\d{2})\.(\d{4})", re.I)


def _reg_date(checks: list[Check]) -> date | None:
    for c in checks:
        if c.key != "object":
            continue
        m = _REG_DATE_RE.search(c.detail or "")
        if m:
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                return None
    return None


def _has_fresh_right(checks: list[Check]) -> bool:
    """Право моложе трёх лет — срок оспаривания сделок прежнего собственника
    ещё не истёк (ст. 196 ГК РФ, ст. 61.2 ФЗ-127)."""
    d = _reg_date(checks)
    return bool(d and (date.today() - d).days < 3 * 365)


def _months_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "месяц"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "месяца"
    return "месяцев"


def _years_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "год"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "года"
    return "лет"


def elapsed_phrase(d: date, today: date | None = None) -> str:
    """«семь месяцев назад», «два года и три месяца назад».

    Считает КОД, а не модель. 07.08.2026 LLM написал про право от 26.12.2025
    «менее трёх месяцев назад» — прошло семь с половиной. Выдуманный срок в
    платном заключении хуже, чем его отсутствие, поэтому готовую формулировку
    даём в промпт, а считать самостоятельно модели прямо запрещаем.
    """
    today = today or date.today()
    months = (today.year - d.year) * 12 + (today.month - d.month)
    if today.day < d.day:
        months -= 1
    months = max(months, 0)
    if months < 1:
        return "меньше месяца назад"
    if months < 12:
        return f"{months} {_months_word(months)} назад"
    years, rest = divmod(months, 12)
    out = f"{years} {_years_word(years)}"
    if rest:
        out += f" и {rest} {_months_word(rest)}"
    return out + " назад"


def _elapsed_hint(checks: list[Check]) -> str:
    """Готовая строка про давность права для промпта — или пусто, если даты нет."""
    d = _reg_date(checks)
    if not d:
        return ""
    return ("\nДАВНОСТЬ ПРАВА (посчитано, используй дословно): право собственности "
            f"зарегистрировано {d.strftime('%d.%m.%Y')}, это {elapsed_phrase(d)}.")


# Дата актуальности стоит в самом тексте блока — её кладёт туда _tail() из
# modules/auto.py. Формат наш собственный и фиксированный, поэтому вынимаем
# её отсюда, а не пробрасываем через полдюжины сигнатур. Меняете _tail —
# правьте и это выражение (оба места помечены словом «актуальны»).
_AS_OF_RE = re.compile(r"актуальны на (\d{2}\.\d{2}\.\d{4})")


def _freshness_note(checks: list[Check]) -> str:
    """Готовый разбор «что свежее, что архивное» — считаем МЫ, не модель.

    08.08.2026 модель написала «все сведения датированы 2023 годом», хотя залог
    был проверен в тот же день. Обобщение обесценивало единственную по-настоящему
    свежую проверку — самую важную из всех. Даём ей факт, а не повод обобщать.
    """
    fresh, stale = [], []
    for c in checks:
        m = _AS_OF_RE.search(c.detail or "")
        if not m:
            continue
        (stale if "⚠️" in (c.detail or "") else fresh).append(f"{c.name} ({m.group(1)})")
    if not fresh and not stale:
        return ""
    out = "\n\nСВЕЖЕСТЬ ДАННЫХ (посчитано, не обобщай):"
    if fresh:
        out += "\n· проверено СВЕЖО: " + "; ".join(fresh)
    if stale:
        out += "\n· данные АРХИВНЫЕ: " + "; ".join(stale)
    return out


def _rule_headline(risk: str, checks: list[Check] | None = None) -> str:
    if risk == RISK_HIGH:
        return "Покупать нельзя: машину заберут или не поставят на учёт"
    if risk == RISK_MEDIUM:
        return "Есть к чему присмотреться перед покупкой"
    # Заголовок обязан согласовываться с телом. На недвижимости 07.08 вышел отчёт
    # с заголовком «рисков не обнаружено» при описанном в тексте риске — клиент
    # читает заголовок, и он не должен противоречить содержимому.
    #
    # Здесь та же проверка на нечестное «всё чисто»: если ключевые блоки не
    # проверились, писать «ничего не найдено» нельзя — не найдено потому, что не
    # искали. Отсутствие записи о ДТП не доказывает, что машина не билась.
    if checks and any(c.status == "not_checked" for c in checks):
        return "По реестрам чисто, но часть данных получить не удалось"
    return "По реестрам ничего не найдено"


def _rule_body(checks: list[Check], risk: str) -> str:
    found = [c for c in checks if c.status == "found"]
    not_checked = [c for c in checks if c.status == "not_checked"]
    ok = [c for c in checks if c.status == "not_found"]

    parts: list[str] = []
    if found:
        parts.append(
            "Обнаружены флаги: "
            + "; ".join(f"{c.name} — {c.detail}" for c in found)
            + "."
        )
    if ok:
        parts.append(
            "Проверено и чисто: " + ", ".join(c.name for c in ok) + "."
        )
    if not_checked:
        parts.append(
            "Не удалось проверить автоматически: "
            + ", ".join(c.name for c in not_checked)
            + ". Требуется ручная проверка на официальных порталах."
        )
    if not parts:
        parts.append("Проверки не дали значимых результатов.")
    return " ".join(parts)


def _llm_body(
    checks: list[Check], risk: str, headline: str, seller_name: str,
    object_kind: str = "",
) -> str | None:
    key = os.getenv("OPENROUTER_API_KEY", "").strip() or os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None

    check_lines = []
    for c in checks:
        marker = {"found": "⚠ обнаружено", "not_found": "✓ чисто", "not_checked": "? не проверено"}.get(c.status, "?")
        check_lines.append(f"[{marker}] {c.name} ({c.source}): {c.detail}")
    # Тип объекта модели надо СКАЗАТЬ. Раньше промпт начинался с «юрист по жилой
    # недвижимости… для покупателя квартиры», и здания с участками — а это 41% наших
    # проверок — в заключении становились «квартирой»: «Квартира с кадастровым
    # номером 86:13:0501002:333» про здание 40 000 м². Тип лежит в детали блока
    # ЕГРН, откуда его и берём.
    # Тип берём из превью (там он всегда есть: «Квартира», «Здание», «Земельный
    # участок»), а деталь блока ЕГРН — запасной путь. В детали лежит НАЗНАЧЕНИЕ
    # («жилое»), и по нему квартиру от здания не отличить: без явного типа
    # заключение по квартире выходило обезличенным «объект недвижимости».
    kind = (object_kind or "").strip().lower() or _object_kind(checks)
    subject = kind or "объект недвижимости"
    prompt = (
        f"Ты — автоэксперт с юридической подготовкой, готовишь для ПОКУПАТЕЛЯ "
        f"подержанного автомобиля заключение по итогам проверки машины по "
        f"государственным реестрам.\n"
        f"ПРОВЕРЯЕМЫЙ ОБЪЕКТ — АВТОМОБИЛЬ{(' ' + subject.upper()) if subject and subject != 'объект недвижимости' else ''}. "
        f"Пиши «автомобиль» или «машина», никогда — «объект недвижимости», "
        f"«квартира», «помещение».\n\n"
        # Здесь стояла ФАМИЛИЯ-ПРИМЕР («Ульянин» — из наших же реквизитов). При пустом
        # ФИО модель принимала пример за данные и объявляла его собственником: 92
        # выданных отчёта из 534 назвали «Ульянина» владельцем чужой квартиры, включая
        # заказ, где клиент указал совсем другого продавца. Примеров имён тут быть
        # не должно — только запрет придумывать.
        + (f"Проверяемый продавец: {seller_name} — это ПРОВЕРЯЕМОЕ лицо, а НЕ адресат "
           "заключения. Упоминай его в третьем лице, по фамилии из этой строки.\n"
           if seller_name else
           "Продавец НЕ НАЗВАН: его данные не проверялись. Пиши обезличенно — "
           "«продавец». СТРОГО ЗАПРЕЩЕНО придумывать фамилию, имя или инициалы "
           "собственника и продавца: если фамилии нет в данных проверок ниже, её "
           "нет вообще, и упоминать любую конкретную фамилию нельзя.\n")
        + "НЕ обращайся к читателю по имени продавца, НЕ "
        "начинай с приветствия или обращения — сразу с вывода.\n"
        f"Итоговый уровень риска покупки: {risk.upper()}\n\n"
        "Результаты проверок по базам:\n" + "\n".join(check_lines) + "\n\n"
        "Напиши заключение на русском языке (4–6 предложений, деловой, но "
        "человеческий тон — как объяснил бы опытный автоподборщик):\n"
        "1. Начни с прямого вывода: брать машину, торговаться или уходить.\n"
        "2. По КАЖДОМУ обнаруженному сигналу (⚠) объясни, чем это грозит "
        "покупателю и на какую норму опирается риск: залог → сохраняется при "
        "смене собственника по ст. 353 ГК, машину заберёт банк; запрет "
        "регистрационных действий → автомобиль не переоформят на покупателя "
        "(№229-ФЗ); розыск или утилизация → на учёт не поставят; ДТП → скрытые "
        "повреждения силовых элементов и потеря стоимости; частая смена "
        "владельцев → скрытая проблема; такси и каршеринг → ускоренный износ.\n"
        "3. Дай 1–2 конкретных следующих шага: что смотреть на осмотре, какие "
        "документы просить у продавца, о чём торговаться.\n"
        "4. По пунктам «не проверено» честно предупреди, что данных нет, и "
        "НЕ выдавай отсутствие записи за отсутствие проблемы: если по машине "
        "нет диагностических карт, это не значит, что пробег не скручен.\n"
        # Свежесть данных — наш главный отличительный признак и главный риск
        # претензий. Модель обязана её проговорить, а не проглотить.
        "5. Если в результатах проверок стоит дата актуальности старше года — "
        "прямо напиши, что эти сведения архивные и перед сделкой их нужно "
        "сверить на портале Госуслуг, это бесплатно. НО не обобщай: у разных "
        "баз даты РАЗНЫЕ. Запрещены формулировки вида «все сведения датированы "
        "2023 годом» — называй устаревшими только те проверки, у которых старая "
        "дата действительно стоит, и отдельно скажи, какие проверены свежо.\n\n"
        "Строго не выдумывай фактов, которых нет в результатах. Без "
        "маркетинга и воды. Не используй markdown-разметку и заголовки — "
        "только связный текст.\n"
        # 08.08.2026 в платный отчёт ушло «запросите historical данные» —
        # английское слово посреди русской фразы. Документ читает покупатель
        # машины, а не разработчик.
        "Пиши ТОЛЬКО по-русски: никаких английских слов и латиницы, кроме "
        "марки, модели и VIN автомобиля.\n\n"
        # 07.08.2026: про право от 26.12.2025 модель написала «менее трёх месяцев
        # назад» — прошло семь с половиной. Она не считает интервалы, а пишет
        # правдоподобное, и выдуманный срок уходит клиенту в платном документе.
        "СРОКИ НЕ ВЫЧИСЛЯЙ. Никогда не переводи даты в интервалы самостоятельно "
        "и не пиши оценок вроде «менее трёх месяцев назад», «около года назад», "
        "«недавно». Сколько прошло времени — только теми словами, которые даны "
        "ниже дословно; если такой строки ниже нет, вообще не упоминай, сколько "
        "прошло, а называй саму дату."
        + _elapsed_hint(checks)
        + _freshness_note(checks)
    )
    model = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4.5")
    payload = {"model": model, "max_tokens": 700,
               "messages": [{"role": "user", "content": prompt}]}
    # Сервер в РФ → западный OpenRouter через иностранный прокси (LLM_PROXY),
    # с фолбэком на прямое соединение, если прокси недоступен.
    import httpx
    proxy = os.getenv("LLM_PROXY", "").strip() or None
    for px in ([proxy, None] if proxy else [None]):
        try:
            with httpx.Client(proxy=px, timeout=40.0) as client:
                resp = client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
            data = resp.json()
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if text:
                return _strip_invented_names(text, seller_name, checks)
        except Exception:
            continue
    return None


# Фамилия, которой нет в исходных данных, не имеет права попасть в заключение.
# Запрета в промпте мало: именно так 92 отчёта объявили собственником чужого
# человека. Здесь — последний рубеж, уже по факту сгенерированного текста.
#
# Ловим УЗКО: имя собственное сразу после «собственности / продавца / продавец».
# Широкий поиск «любое слово с заглавной» вырезал бы «Объект», «Росреестр» и
# начала предложений — лечение оказалось бы хуже болезни.
# БЕЗ re.I: флаг отменял требование заглавной буквы в третьей группе, и фильтр
# вырезал ЛЮБОЕ слово после «продавца/собственности/владельца» — в живом отчёте
# так пропала «выписку» из фразы «запросите свежую выписку из ЕГРН». Ключевые
# слова пишем в обоих регистрах явно: заголовок предложения тоже бывает.
_OBJECT_KINDS = {
    "квартира": "квартира", "комната": "комната", "помещение": "помещение",
    "здание": "здание", "сооружение": "сооружение", "машино-место": "машино-место",
    "земельный участок": "земельный участок", "участок": "земельный участок",
}


def _object_kind(checks: list) -> str:
    """Тип объекта из блока ЕГРН: «здание», «земельный участок», «квартира»…

    Берём из уже собранной детали проверки, а не заводим новый параметр: деталь
    формируется из того же ответа реестра и всегда доходит сюда, даже когда
    вердикт строится из сохранённого отчёта.
    """
    for c in checks or []:
        if getattr(c, "key", "") != "object":
            continue
        det = str(getattr(c, "detail", "") or "").lower()
        for word, norm in _OBJECT_KINDS.items():
            if word in det:
                return norm
    return ""


_OWNER_RE = re.compile(
    r"([Сс]обственност\w*|[Пп]родавц\w*|[Пп]родавец|[Вв]ладельц\w*|[Вв]ладелец)"
    r"(\s+)([А-ЯЁ][а-яё]{2,})")


def _strip_invented_names(text: str, seller_name: str, checks: list[Check]) -> str:
    """Убрать фамилию собственника, если её нет ни во вводе, ни в данных проверок."""
    known = (seller_name or "").lower()
    for c in checks or []:
        known += " " + str(getattr(c, "detail", "") or "").lower()
    bad: list[str] = []

    def _fix(m: re.Match) -> str:
        w = m.group(3)
        # Сверяем по корню: «Ульянина» в тексте и «Ульянин» во вводе — одно лицо.
        if w.lower()[:6] in known:
            return m.group(0)
        bad.append(w)
        return m.group(1)          # оставляем «собственности», имя убираем

    out = _OWNER_RE.sub(_fix, text or "")
    if bad:
        print(f"[verdict] выдуманные фамилии вырезаны: {sorted(set(bad))[:5]}", flush=True)
    return out


def _llm_openrouter(prompt: str, max_tokens: int) -> str:
    """Один вызов OpenRouter (через прокси с фолбэком). Пусто при ошибке/без ключа."""
    key = os.getenv("OPENROUTER_API_KEY", "").strip() or os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return ""
    model = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4.5")
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "user", "content": prompt}]}
    import httpx
    proxy = os.getenv("LLM_PROXY", "").strip() or None
    for px in ([proxy, None] if proxy else [None]):
        try:
            with httpx.Client(proxy=px, timeout=40.0) as client:
                resp = client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=payload,
                )
            text = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if text:
                return text
        except Exception:
            continue
    return ""


def inn_analysis(seller_name: str, checks: list[Check]) -> str:
    """Отдельный LLM-анализ доп-проверки по ИНН (банкротство + арбитраж)."""
    lines = []
    for c in checks:
        m = {"found": "⚠ обнаружено", "not_found": "✓ чисто", "not_checked": "? не проверено"}.get(c.status, "?")
        lines.append(f"[{m}] {c.name}: {c.detail}")
    prompt = (
        "Ты — юрист по сделкам с жилой недвижимостью. Покупатель ДОПЛАТИЛ за "
        "углублённую проверку продавца по ИНН — банкротство (реестр Федресурса) "
        "и арбитражные дела. Проверяемый продавец: "
        f"{seller_name or 'не указан'} — это ПРОВЕРЯЕМОЕ лицо, не адресат.\n\n"
        "Результаты проверки по ИНН:\n" + "\n".join(lines) + "\n\n"
        "Напиши для ПОКУПАТЕЛЯ краткий вывод (2–4 предложения) ИМЕННО по этим двум "
        "проверкам: что означают результаты (банкротство → риск оспаривания сделки "
        "по ст. 61.2 №127-ФЗ и возврата квартиры кредиторам; арбитраж → банкротные "
        "заявления и корпоративные споры), есть ли повод для беспокойства и что делать. "
        "Не обращайся к читателю по имени, без приветствия, без markdown — только "
        "связный текст."
    )
    return _llm_openrouter(prompt, 400) or None


def build_verdict(checks: list[Check], seller_name: str = "",
                  object_kind: str = "") -> Verdict:
    # Заблокированные блоки (базовый тариф) в вердикт не идут вовсе: мы их не
    # показываем, значит и рассуждать о них — ни модели, ни правилам — нельзя.
    # Иначе в тексте появится «ДТП не найдено» по блоку, который человек не купил.
    checks = [c for c in checks if c.status != "locked"]
    risk = compute_risk(checks)
    headline = _rule_headline(risk, checks)
    recs = _rule_recommendations(checks, risk)

    llm_body = _llm_body(checks, risk, headline, seller_name, object_kind)
    if llm_body:
        return Verdict(
            risk=risk,
            headline=headline,
            body=llm_body,
            recommendations=recs,
            llm_used=True,
        )
    return Verdict(
        risk=risk,
        headline=headline,
        body=_rule_body(checks, risk),
        recommendations=recs,
        llm_used=False,
    )
