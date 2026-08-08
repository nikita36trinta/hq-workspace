"""Проверки автомобиля через apipoint: сырой ответ → наши Check-объекты.

ГЛАВНОЕ ПРО ЭТОТ МОДУЛЬ — ЧЕСТНОСТЬ ПО ДАТАМ.

07.08.2026 замерено: apipoint отдаёт живыми только юридические реестры (залог
ФНП опрошен в момент запроса), а всё гибддшное — кэш. В одном отчёте: ДТП
запрошено 01.01.2023, ограничения 24.03.2023, история регистраций 15.08.2023,
ЕАИСТО по конкретному VIN — снимок от 13.03.2022 при том, что машина проходила
техосмотр 27.02.2026. Источник на гибдд.рф закрыт для парсеров, и api-assist
подтвердил это письменно.

Поэтому КАЖДЫЙ блок несёт дату актуальности, и она попадает в текст отчёта:
«ДТП не найдено (по данным на 01.01.2023)», а не «ДТП не найдено». Это не
косметика: продавать трёхлетний снимок как сегодняшнюю проверку — ровно то, за
что мы сами сегодня требовали возврат у конкурента. Размеченный старый факт
продавать можно, выданный за свежий — нет.

Где даты брать:
  * reportjson отдаёт блок RequestDate с датой обращения к каждому источнику;
  * поштучные методы отдают requestDate внутри своего результата;
  * если даты нет — пишем «дата неизвестна», а не молчим.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# Клиент лежит в общем hq-workspace/auto — тот же, что использовался для замеров.
try:  # рабочий импорт внутри контейнера
    from modules import apipoint
except ImportError:  # локальная отладка из hq-workspace
    import apipoint  # type: ignore


# ── что показываем в отчёте ────────────────────────────────────────────────
# Порядок = порядок в отчёте. Первым идёт то, из-за чего сделка срывается.
AUTO_CHECKS: list[tuple[str, str, str]] = [
    ("restrict",  "Ограничения на регистрацию", "ГИБДД"),
    ("pledge",    "Залог и лизинг",             "Реестр залогов ФНП, Федресурс"),
    ("wanted",    "Розыск",                     "ГИБДД"),
    ("dtp",       "ДТП",                        "ГИБДД"),
    ("mileage",   "Пробег и скрутки",           "ЕАИСТО, диагностические карты"),
    ("history",   "История регистраций",        "ГИБДД"),
    ("taxi",      "Работа в такси и каршеринге","ФГИС «Такси», операторы каршеринга"),
    ("customs",   "Таможня и утилизация",       "ФТС"),
]


@dataclass
class AutoCheck:
    """Один блок отчёта. `as_of` — дата, НА КОТОРУЮ верны данные."""
    key: str
    name: str
    source: str
    status: str                      # found | not_found | not_checked
    detail: str
    items: list[dict[str, Any]] = field(default_factory=list)
    as_of: date | None = None
    stale: bool = False              # данные старше порога — предупреждаем явно


# Порог, после которого данные считаем устаревшими и говорим об этом громко.
# Год выбран не наугад: диагностическая карта действует до двух лет, регистрация
# меняется реже, а вот полугодовой давности ДТП покупателю уже критично.
STALE_AFTER_DAYS = 365


def _as_of(value: Any) -> date | None:
    """Дата актуальности из ответа. Понимает ISO и /Date(миллисекунды)/."""
    if not value:
        return None
    s = str(value)
    m = re.search(r"/Date\((-?\d+)\)/", s)
    if m:
        try:
            ts = int(m.group(1)) / 1000
            if ts < 0:                       # заглушка 0001-01-01
                return None
            return datetime.utcfromtimestamp(ts).date()
        except (ValueError, OSError, OverflowError):
            return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        if y < 1900:                          # 0001-01-01 = «не запрашивалось»
            return None
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    return None


def _human(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "дата неизвестна"


def _stale(d: date | None, today: date | None = None) -> bool:
    if not d:
        return True                           # даты нет — считаем несвежим
    return ((today or date.today()) - d).days > STALE_AFTER_DAYS


def _tail(as_of: date | None) -> str:
    """Хвост про актуальность — в КАЖДОЙ строке отчёта, без исключений."""
    if not as_of:
        return " Дата актуальности данных неизвестна — проверьте сведения в ГИБДД перед сделкой."
    if _stale(as_of):
        return (f" ⚠️ Данные актуальны на {_human(as_of)} — это больше года назад. "
                f"Сведения могли измениться; перед сделкой проверьте в ГИБДД.")
    return f" Данные актуальны на {_human(as_of)}."


# ── разбор блоков reportjson ───────────────────────────────────────────────

def _dig(data: dict[str, Any], *path: str) -> Any:
    cur: Any = data
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def from_report(report: dict[str, Any]) -> list[AutoCheck]:
    """reportjson (50 ₽) → список блоков отчёта с датами актуальности."""
    data = _dig(report, "result", "reportjson", "Result", "data") or {}
    rq = data.get("RequestDate") or {}

    d_dtp = _as_of(rq.get("Dtp"))
    d_restrict = _as_of(rq.get("Restrict"))
    d_hist = _as_of(rq.get("RegHistory"))
    d_pledge = _as_of(_dig(rq, "Zalog", "ZalogRequestDate"))

    out: list[AutoCheck] = []

    def add(key: str, status: str, detail: str, as_of: date | None,
            items: list | None = None) -> None:
        name, source = next(((n, s) for k, n, s in AUTO_CHECKS if k == key),
                            (key, ""))
        out.append(AutoCheck(key=key, name=name, source=source, status=status,
                             detail=detail + _tail(as_of), items=items or [],
                             as_of=as_of, stale=_stale(as_of)))

    # Ограничения — первое, что срывает постановку на учёт.
    restr = _dig(data, "GibddRestrict", "Rows") or []
    add("restrict",
        "found" if restr else "not_found",
        (f"Действующих ограничений: {len(restr)}. Поставить на учёт не получится, "
         f"пока их не снимут." if restr else "Ограничений на регистрационные действия не найдено."),
        d_restrict, restr)

    # Залог и лизинг — единственный блок, который у поставщика живой.
    zalog_n = data.get("ZalogCount") or 0
    leasing = data.get("LeasingInfo")
    add("pledge",
        "found" if (zalog_n or leasing) else "not_found",
        ("В реестре залогов есть записи по этому автомобилю — при неоплаченном "
         "кредите машину может забрать банк." if zalog_n else
         "Автомобиль не числится в залоге и в лизинге."),
        d_pledge)

    add("wanted",
        "found" if data.get("HasRestrict") and data.get("IsTotalCar") else "not_found",
        "Автомобиль не числится в розыске." if not data.get("IsTotalCar")
        else "Есть сведения о розыске — сделка невозможна.",
        d_restrict)

    dtp = _dig(data, "GibddDtp", "Accindents") or []
    add("dtp",
        "found" if dtp else "not_found",
        (f"Зафиксировано ДТП: {len(dtp)}. Проверьте качество ремонта и "
         f"геометрию кузова." if dtp else "ДТП по базе ГИБДД не найдено."),
        d_dtp, dtp)

    # Пробег: у поставщика этот блок чаще всего пуст — говорим прямо.
    probeg = data.get("ProbegList") or []
    last = data.get("Probeg") or 0
    d_probeg = _as_of(data.get("ProbegDate"))
    if probeg or last:
        add("mileage", "found",
            (f"Последний зафиксированный пробег: {last:,} км. Записей в истории: "
             f"{len(probeg)}.".replace(",", " ") +
             ("" if len(probeg) > 1 else
              " Записей меньше двух — по ним нельзя судить о скрутке.")),
            d_probeg, probeg)
    else:
        add("mileage", "not_checked",
            "Данных о пробеге и диагностических картах в источнике нет. "
            "Это НЕ означает, что их нет в природе: запросите у продавца "
            "диагностическую карту или проверьте на портале ГИБДД.",
            d_probeg)

    hist = _dig(data, "GidbbRegHistory") or {}
    total = data.get("GibddListTotal") or 0
    add("history",
        "found" if total else "not_checked",
        (f"Записей о регистрационных действиях: {total}."
         if total else "История регистраций в источнике не найдена."),
        d_hist)

    taxi = bool(data.get("HasTaxi") or data.get("HasRsaTaxi"))
    car_sh = bool(data.get("UsedInCarsharing"))
    add("taxi",
        "found" if (taxi or car_sh) else "not_found",
        ("Автомобиль использовался в такси или каршеринге — износ у таких машин "
         "заметно выше обычного." if (taxi or car_sh) else
         "Сведений об использовании в такси и каршеринге нет."),
        d_hist)

    customs = data.get("CustomsItems") or []
    util = bool(data.get("IsTotalCar"))
    add("customs",
        "found" if customs else "not_found",
        (f"Таможенных деклараций: {len(customs)}. Автомобиль ввозился из-за границы."
         if customs else "Сведений о таможенном оформлении нет.") +
        (" Автомобиль числится утилизированным — поставить на учёт нельзя." if util else ""),
        d_dtp, customs)

    return out


def summary(checks: list[AutoCheck]) -> dict[str, Any]:
    """Сводка для экрана оплаты и для вердикта."""
    found = [c for c in checks if c.status == "found"]
    stale = [c for c in checks if c.stale and c.status != "not_checked"]
    missing = [c for c in checks if c.status == "not_checked"]
    return {
        "problems": len(found),
        "stale_blocks": len(stale),
        "missing_blocks": len(missing),
        # Самая старая дата по отчёту — её честно показываем на видном месте.
        "oldest": min((c.as_of for c in checks if c.as_of), default=None),
    }


# ── распознавание ввода ────────────────────────────────────────────────────
# VIN: 17 знаков, без I, O, Q — их исключили, чтобы не путать с 1 и 0.
_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.I)
# Госномер РФ: только те 12 кириллических букв, что совпадают с латиницей.
_PLATE_RE = re.compile(r"^[АВЕКМНОРСТУХ]\d{3}(?!000)[АВЕКМНОРСТУХ]{2}\d{2,3}$", re.I)
# Человек часто набирает номер латиницей — раскладка та же, буквы похожи.
_LAT2CYR = str.maketrans("ABEKMHOPCTYXabekmhopctyx", "АВЕКМНОРСТУХАВЕКМНОРСТУХ")


def normalize_ref(raw: str) -> tuple[str, str]:
    """Что ввёл человек → ('vin'|'plate'|'', нормализованное значение).

    Пустой тип означает «не распознали»: спрашивать надо ДО оплаты, а не
    выяснять после неё, что проверять нечего.
    """
    s = re.sub(r"[\s\-—]+", "", (raw or "")).upper()
    if not s:
        return "", ""
    if _VIN_RE.match(s):
        return "vin", s
    plate = s.translate(_LAT2CYR)
    if _PLATE_RE.match(plate):
        return "plate", plate
    return "", s


def preview(raw: str) -> dict[str, Any]:
    """Бесплатный экран «мы нашли вашу машину». Стоит 1,10 ₽ по VIN.

    По госномеру дороже вдвое (сначала конвертация), и это осознанно: в форме
    просим VIN, госномер оставляем запасным путём. Дешевле опознания на рынке
    нет ни у кого — проверено 07.08.2026 по всем поставщикам с публичными ценами.
    """
    kind, ref = normalize_ref(raw)
    if not kind:
        return {"found": False, "why": "not_recognized", "ref": ref}
    try:
        got = apipoint.identify(vin=ref if kind == "vin" else "",
                                plate=ref if kind == "plate" else "")
    except apipoint.NoFunds:
        return {"found": False, "why": "no_funds", "ref": ref}
    except apipoint.ApiPointError as e:
        return {"found": False, "why": f"source_error:{e}", "ref": ref}
    if not got.get("found"):
        return {"found": False, "why": "not_in_registry", "ref": ref}
    # Реальная форма ответа vindecode (проверена на живом VIN 08.08.2026):
    # result.vindecode.decode.Reports[0].Data — там Brand/Model/BodyName и
    # вложенные словари EnginePower/EngineVolume. Плоских ключей нет вовсе.
    d = got.get("data") or {}
    inner = d.get("vindecode") if isinstance(d, dict) else None
    decode = (inner or {}).get("decode") if isinstance(inner, dict) else None
    reports = (decode or {}).get("Reports") if isinstance(decode, dict) else None
    data = reports[0].get("Data") if isinstance(reports, list) and reports else {}
    if not isinstance(data, dict):
        data = {}
    if not data.get("Brand"):
        # Декодер ответил, но машину не разобрал. Показывать пустую карточку
        # «ваш автомобиль: —» хуже, чем честно сказать «не опознали».
        return {"found": False, "why": "not_decoded", "ref": ref}

    vol = data.get("EngineVolume") or {}
    pwr = data.get("EnginePower") or {}
    litres = vol.get("L") if isinstance(vol, dict) else None
    hp = pwr.get("Hp") if isinstance(pwr, dict) else None
    engine = " ".join(x for x in (
        f"{litres} л" if litres else "",
        f"{round(float(hp))} л.с." if hp else "",
        _fuel_ru(data.get("FuelType")),
    ) if x).strip()

    return {
        "found": True, "kind": kind, "vin": got.get("vin") or ref,
        "marka": data.get("Brand"),
        "model": " ".join(str(x) for x in (data.get("Model"),
                                          data.get("Modification")) if x),
        # VIN-декодер даёт ГОД ПОКОЛЕНИЯ, а не год выпуска конкретной машины
        # (ModelYear почти всегда null). Выдать его за год выпуска — соврать
        # человеку в первом же экране, поэтому помечаем словом «с».
        "year": data.get("ModelYear") or (f"с {data['StartYear']}"
                                          if data.get("StartYear") else None),
        "engine": engine or None,
        "body": data.get("BodyName") or data.get("Body"),
        "raw": data,
    }


_FUEL_RU = {"D": "дизель", "P": "бензин", "G": "газ",
            "E": "электро", "H": "гибрид"}


def _fuel_ru(v: Any) -> str:
    """Код топлива из декодера («D») → слово, понятное покупателю."""
    if isinstance(v, list):
        v = v[0] if v else ""
    return _FUEL_RU.get(str(v or "").strip().upper(), "")


def _first(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0):
            return v
    return None


def run_full(raw: str, basic: bool = False) -> list[AutoCheck]:
    """Полная проверка после оплаты: один reportjson за 50 ₽ → блоки отчёта.

    basic=True — тариф за 199 ₽: тот же запрос (дешевле он не станет), но в
    отчёт уходят только стоп-факторы. Экономии на запросе тут нет и не может
    быть: поставщик отдаёт отчёт целиком одним вызовом.

    Поштучные методы отдельно не дёргаем: замер 07.08.2026 показал, что
    агрегированный отчёт содержит те же данные, а стоит дешевле суммы частей.
    Поллинг у поставщика бесплатный — проверено, списывается только создание.
    """
    kind, ref = normalize_ref(raw)
    if not kind:
        return [AutoCheck(key=k, name=n, source=s, status="not_checked",
                          detail="Не удалось распознать VIN или госномер.", as_of=None,
                          stale=True) for k, n, s in AUTO_CHECKS]
    vin = ref
    if kind == "plate":
        got = apipoint.call("number2vin", {"gosnomer": ref})
        vin = str(((got.get("result") or {}).get("number2vin") or {}).get("vin") or "").upper()
        if not _VIN_RE.match(vin or ""):
            return [AutoCheck(key=k, name=n, source=s, status="not_checked",
                              detail="По госномеру VIN не найден — пришлите VIN.",
                              as_of=None, stale=True) for k, n, s in AUTO_CHECKS]
    created = apipoint.call("reportjson", {"mode": "create", "vin": vin})
    task = ((created.get("result") or {}).get("reportjson") or {}).get("Task") or {}
    tid = task.get("ID")
    if not tid:
        raise apipoint.ApiPointError("отчёт не создан: нет ID задачи")
    import time as _t
    for _ in range(30):                     # до 5 минут, опросы бесплатны
        _t.sleep(10)
        ch = apipoint.call("reportjson", {"mode": "check", "id": tid}, attempts=1)
        st = (((ch.get("result") or {}).get("reportjson") or {}).get("Task") or {}).get("Status")
        if str(st) == "1":
            break
    checks = from_report(apipoint.call("reportjson", {"mode": "result", "id": tid}))
    return _apply_tier(checks, basic=basic)


# Базовый тариф отдаёт три блока, которые решают «покупать или нет»: залог,
# ограничения на регистрацию и розыск. Это ровно те три, по которым строится
# стоп-вердикт (_AUTO_STOP в verdict.py) — то есть за 199 ₽ человек получает
# полноценный ответ на главный вопрос, а не огрызок. История (ДТП, пробег,
# такси, таможня, регистрации) — это «как жила машина», и она в полном тарифе.
BASIC_KEYS = frozenset({"pledge", "restrict", "wanted"})


def _apply_tier(checks: list["AutoCheck"], basic: bool) -> list["AutoCheck"]:
    """В базовом тарифе прячем историю, но не выбрасываем блоки из отчёта.

    Блок остаётся в списке со статусом locked и честной подписью: человек
    видит, что именно он не купил, и может дозаказать. Молча вырезать строки
    нельзя — тогда два тарифа выглядят одинаково, и разница в 250 ₽ ничем
    не объяснена.
    """
    if not basic:
        return checks
    out = []
    for c in checks:
        if c.key in BASIC_KEYS:
            out.append(c)
            continue
        out.append(AutoCheck(key=c.key, name=c.name, source=c.source,
                             status="locked", items=[], as_of=c.as_of,
                             stale=c.stale,
                             detail="Входит в полный отчёт — 449 ₽."))
    return out


def recheck_pledge(raw: str) -> AutoCheck:
    """Повторная проверка ТОЛЬКО залога — перед передачей денег.

    Отдельный дешёвый метод (1,90 ₽) вместо полного отчёта (50 ₽): здесь нужен
    один факт, а не история. Смысл услуги в том, что запись о залоге появляется
    в реестре ФНП в день оформления, и между осмотром и сделкой машина могла
    уйти в микрозайм — осмотр этого не покажет никогда.
    """
    kind, ref = normalize_ref(raw)
    if not kind:
        return AutoCheck(key="pledge", name="Залог и лизинг", source="Реестр залогов ФНП",
                         status="not_checked", detail="Не удалось распознать VIN или госномер.",
                         as_of=None, stale=True)
    vin = ref
    if kind == "plate":
        got = apipoint.call("number2vin", {"gosnomer": ref})
        vin = str(((got.get("result") or {}).get("number2vin") or {}).get("vin") or "").upper()
        if not _VIN_RE.match(vin or ""):
            return AutoCheck(key="pledge", name="Залог и лизинг", source="Реестр залогов ФНП",
                             status="not_checked", detail="По госномеру VIN не найден — пришлите VIN.",
                             as_of=None, stale=True)
    out = apipoint.call("notary", {"vin": vin})
    res = ((out.get("result") or {}).get("notary") or {})
    today = date.today()
    num = res.get("num")
    # Ответ без поля num — не «залога нет», а «источник не ответил как ожидалось».
    # Выдать молчание за чистоту здесь дороже всего: ровно ради этого факта
    # человек и заплатил ещё раз, прямо перед передачей денег.
    if num is None:
        return AutoCheck(key="pledge", name="Залог и лизинг", source="Реестр залогов ФНП",
                         status="not_checked",
                         detail="Реестр залогов не ответил — повторим проверку бесплатно, "
                                "напишите нам." + _tail(None),
                         as_of=None, stale=True)
    if int(num) > 0:
        return AutoCheck(key="pledge", name="Залог и лизинг", source="Реестр залогов ФНП",
                         status="found",
                         detail=f"НАЙДЕН ЗАЛОГ: записей в реестре — {int(num)}. Деньги не "
                                f"передавайте: по ст. 353 ГК залог сохраняется при смене "
                                f"собственника, и машину заберут уже у вас." + _tail(today),
                         items=[], as_of=today)
    return AutoCheck(key="pledge", name="Залог и лизинг", source="Реестр залогов ФНП",
                     status="not_found",
                     detail="Автомобиль не числится в залоге." + _tail(today),
                     items=[], as_of=today)


# ── мост к формату приложения ──────────────────────────────────────────────
# Приложение (и PDF, и страница отчёта, и вердикт) умеет работать с Check из
# modules.verdict. Отдаём наши блоки в этом виде, чтобы не переписывать вывод.

def to_legacy_checks(checks: list["AutoCheck"]) -> list[Any]:
    from modules.verdict import Check
    return [Check(key=c.key, name=c.name, source=c.source, status=c.status,
                  detail=c.detail, items=c.items) for c in checks]


def preview_checks(raw: str) -> list["AutoCheck"]:
    """Список блоков для БЕСПЛАТНОГО экрана: что проверим после оплаты.

    Ни одного платного вызова здесь нет, кроме опознания машины (1,10 ₽) —
    оно идёт отдельно, в preview(). Логика та же, что на недвижимости: не
    тратим деньги на тех, кто не купит.
    """
    return [AutoCheck(key=k, name=n, source=s, status="pending",
                      detail="Проверяется в полном отчёте после оплаты",
                      as_of=None, stale=False)
            for k, n, s in AUTO_CHECKS]
