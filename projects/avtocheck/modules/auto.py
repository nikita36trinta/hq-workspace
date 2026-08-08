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

import json
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
    # Дату сюда раньше подставляли от истории регистраций — от чужого источника.
    # Получалось «сведений о такси нет, данные актуальны на 15.08.2023», хотя к
    # такси эта дата отношения не имеет. Отдельный метод taxi у поставщика на
    # 08.08.2026 отвечает «источник не подключен», то есть за реестром такси мы
    # не ходили вовсе — значит и даты у нас нет. Пишем это прямо.
    if taxi or car_sh:
        add("taxi", "found",
            "Автомобиль использовался в такси или каршеринге — износ у таких "
            "машин заметно выше обычного.", None)
    else:
        add("taxi", "not_found",
            "В агрегированном отчёте отметки о работе в такси и каршеринге нет. "
            "Прямой доступ к реестру такси у поставщика сейчас закрыт, поэтому "
            "отсутствие отметки — не доказательство: проверьте машину в "
            "региональном реестре такси, это бесплатно.", None)

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


# ── сборка отчёта из поштучных методов ─────────────────────────────────────
# Замер 08.08.2026. reportjson (50 ₽) отдаёт всё гибддшное КЭШЕМ 2023 года.
# Метод gai (21 ₽) на тот же VIN вернул Date=08.08.2026 11:31 — живой запрос, и
# в нём сразу ограничения, розыск, история владения и паспорт машины. Остальное
# добираем дешёвыми методами. Итог ≈ 32 ₽ против 50 ₽: дешевле, свежее и шире.
#
# Порядок в списке = порядок блоков в отчёте, от «сделку нельзя» к «влияет на цену».

def _gai_date(s: str) -> date | None:
    """«08.08.2026 11:31:03» → date. Формат источника, не ISO."""
    try:
        return datetime.strptime(str(s)[:10], "%d.%m.%Y").date()
    except Exception:  # noqa: BLE001
        return None


def _call_quiet(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Вызов, который не роняет отчёт. Молчание источника — не повод потерять
    остальные семь блоков, за которые человек заплатил."""
    try:
        got = apipoint.call(method, params, attempts=1)
    except Exception as exc:  # noqa: BLE001
        print(f"[alacarte] {method}: {type(exc).__name__}: {exc}", flush=True)
        return None
    return (got.get("result") or {}).get(method)


def run_alacarte(raw: str, basic: bool = False) -> tuple[list[AutoCheck], dict[str, Any]]:
    """Полный отчёт из поштучных методов. → (блоки, дополнение).

    Дополнение — паспорт машины и периоды владения: они приходят тем же вызовом
    gai, что и ограничения, и отдельных денег не стоят.
    """
    kind, ref = normalize_ref(raw)
    if not kind:
        empty = [AutoCheck(key=k, name=n, source=s, status="not_checked",
                           detail="Не удалось распознать VIN или госномер.",
                           as_of=None, stale=True) for k, n, s in AUTO_CHECKS]
        return empty, {}
    vin = ref
    if kind == "plate":
        got = _call_quiet("number2vin", {"gosnomer": ref})
        vin = str((got or {}).get("vin") or "").upper()
        if not _VIN_RE.match(vin or ""):
            empty = [AutoCheck(key=k, name=n, source=s, status="not_checked",
                               detail="По госномеру VIN не найден — пришлите VIN.",
                               as_of=None, stale=True) for k, n, s in AUTO_CHECKS]
            return empty, {}

    out: list[AutoCheck] = []

    def add(key: str, status: str, detail: str, as_of: date | None,
            items: list | None = None) -> None:
        name, source = next(((n, s) for k, n, s in AUTO_CHECKS if k == key), (key, ""))
        out.append(AutoCheck(key=key, name=name, source=source, status=status,
                             detail=detail + _tail(as_of), items=items or [],
                             as_of=as_of, stale=_stale(as_of)))

    # ── gai: ограничения, розыск, история владения, паспорт. Живой запрос. ──
    gai = _call_quiet("gai", {"vin": vin}) or {}
    g = gai.get("data") or {}
    g_date = _gai_date(g.get("Date") or "")

    if not g:
        add("restrict", "not_checked",
            "Источник ГИБДД не ответил — ограничения проверить не удалось.", None)
        add("wanted", "not_checked",
            "Источник ГИБДД не ответил — розыск проверить не удалось.", None)
    else:
        restr = g.get("Restrict")
        rows = restr if isinstance(restr, list) else ([] if restr in (None, "") else [restr])
        add("restrict", "found" if rows else "not_found",
            (f"Действующих ограничений: {len(rows)}. Поставить машину на учёт не "
             f"получится, пока их не снимут." if rows
             else "Ограничений на регистрационные действия не найдено."),
            g_date, rows if isinstance(restr, list) else [])
        in_search = bool(g.get("InSearch"))
        add("wanted", "found" if in_search else "not_found",
            ("Автомобиль числится в розыске — сделка невозможна." if in_search
             else "Автомобиль не числится в розыске."), g_date)

    # ── залог: живой реестр ФНП, главный риск покупки ────────────────────────
    nz = _call_quiet("notary", {"vin": vin})
    if nz is None:
        add("pledge", "not_checked",
            "Реестр залогов не ответил — это главный риск покупки, проверьте "
            "вручную на reestr-zalogov.ru, там бесплатно.", None)
    else:
        num = nz.get("num")
        lz = _call_quiet("leasing", {"vin": vin}) or {}
        in_lease = bool(lz.get("f") or lz.get("result") not in (None, "", "Данные не найдены", []))
        if num is None:
            add("pledge", "not_checked",
                "Реестр залогов ответил неожиданным образом — проверьте вручную "
                "на reestr-zalogov.ru.", None)
        elif int(num) > 0 or in_lease:
            add("pledge", "found",
                (f"Найдены записи в реестре залогов: {int(num)}. " if int(num) else "") +
                ("Автомобиль числится в лизинге. " if in_lease else "") +
                "По ст. 353 ГК залог сохраняется при смене собственника — машину "
                "заберут уже у вас.", date.today())
        else:
            add("pledge", "not_found",
                "Автомобиль не числится в залоге и в лизинге.", date.today())

    # ── ДТП: у поставщика помечен как кэш, дату отдаёт не всегда ─────────────
    dtp = _call_quiet("dtp", {"vin": vin}) or {}
    acc = ((dtp.get("dtpData") or {}).get("accident")) or []
    d_dtp = _gai_date(dtp.get("requestDate") or dtp.get("requestTime") or "")
    add("dtp", "found" if acc else "not_found",
        (f"Зафиксировано ДТП: {len(acc)}. Проверьте качество ремонта и геометрию "
         f"кузова." if acc else "ДТП по базе ГИБДД не найдено."), d_dtp, acc)

    # ── пробег ───────────────────────────────────────────────────────────────
    pb = _call_quiet("probeg", {"vin": vin}) or {}
    m = ((pb.get("result") or {}).get("m_probeg")) or {}
    km = int(m.get("Probeg") or 0)
    if km > 0:
        add("mileage", "found",
            f"Последний зафиксированный пробег: {km:,} км ({m.get('SourceName') or 'источник не указан'}). "
            f"Сверьте с одометром: значение ниже — признак скрутки.".replace(",", " "),
            _gai_date(m.get("DateString") or ""), [m])
    else:
        eai = _call_quiet("eaisto", {"vin": vin}) or {}
        cards = eai.get("result") if isinstance(eai.get("result"), list) else []
        add("mileage", "found" if cards else "not_checked",
            (f"Диагностических карт найдено: {len(cards)}." if cards else
             "Данных о пробеге и диагностических картах в источниках нет. Это НЕ "
             "означает, что их нет в природе: запросите у продавца диагностическую "
             "карту."),
            _gai_date(eai.get("requestDate") or ""), cards)

    # ── история регистраций: считаем по живой истории из gai ────────────────
    # «Записей: 3» — цифра, из которой покупатель ничего не извлечёт. Считаем
    # то, ради чего эту секцию и смотрят: короткие периоды владения. Машину,
    # которую перепродали дважды за пару месяцев, обычно сбрасывают не просто так.
    hist = g.get("History") or []
    short = 0
    for p in hist:
        if not isinstance(p, dict):
            continue
        a, b = _gai_date(p.get("From") or ""), _gai_date(p.get("To") or "")
        if a and b and (b - a).days < 180:
            short += 1
    if hist:
        detail = f"Записей о регистрационных действиях: {len(hist)}."
        if short:
            detail += (f" Из них {short} владел{'ец' if short == 1 else 'ьцев'} "
                       f"держал{'' if short == 1 else 'и'} машину меньше полугода — "
                       f"так обычно сбрасывают проблемный автомобиль. Спросите "
                       f"продавца, почему он продаёт, и сверьте ответ с датами.")
        add("history", "found", detail, g_date, hist)
    else:
        add("history", "not_checked", "История регистраций в источнике не найдена.",
            g_date, [])

    # ── такси: прямой источник у поставщика закрыт, честно об этом ──────────
    add("taxi", "not_checked",
        "Прямой доступ к реестру такси у поставщика закрыт, поэтому этот пункт "
        "мы не проверяли. Если машина могла работать в такси, сверьтесь в "
        "региональном реестре — это бесплатно.", None)

    # ── таможня и утилизация ─────────────────────────────────────────────────
    cu = _call_quiet("customs", {"vin": vin}) or {}
    cu_items = cu.get("result") if isinstance(cu.get("result"), list) else []
    ut = _call_quiet("utilization", {"vin": vin}) or {}
    ut_found = str((ut or {}).get("result") or "") not in ("", "Данные не найдены")
    add("customs", "found" if (cu_items or ut_found) else "not_found",
        ((f"Таможенных деклараций: {len(cu_items)}. Автомобиль ввозился из-за границы. "
          if cu_items else "Сведений о таможенном оформлении нет. ") +
         ("Автомобиль числится утилизированным — поставить на учёт нельзя."
          if ut_found else "")).strip(),
        _gai_date(cu.get("requestDate") or ""), cu_items)

    passport = {}
    owners = []
    if g:
        passport = {k: v for k, v in {
            "model": (g.get("MarkaModel") or "").strip(),
            "year": str(g.get("Year") or "").strip(),
            "color": (g.get("Color") or "").strip().capitalize(),
            "volume": _pass_num(g.get("EngineVolume")),
            "power_hp": _pass_num(g.get("PowerHp")),
            "category": (g.get("Category") or "").strip(),
            "eco": (g.get("EcoClass") or "").strip(),
            "mass": _pass_num(g.get("Mass")),
            "as_of": g_date.isoformat() if g_date else "",
        }.items() if v}
        # gai отдаёт даты как «31.07.2020», отчёт ждёт ISO. Приводим здесь, а не
        # на фронте: иначе один и тот же формат разбирался бы в двух местах.
        def _iso(s: str) -> str:
            d = _gai_date(s)
            return d.isoformat() if d else ""
        for p in hist:
            if not isinstance(p, dict):
                continue
            owners.append({"kind": (p.get("PersonType") or "Не указано").strip(),
                           "from": _iso(p.get("From") or ""),
                           "to": _iso(p.get("To") or ""),
                           "op": (p.get("LastOperation") or "").strip()})

    out = _apply_tier(out, basic=basic)
    return out, {"passport": passport, "owners": owners}


# ── паспорт машины и периоды владения ──────────────────────────────────────
# Оба берутся ОДНИМ вызовом gibddhistory за 2,10 ₽ — проверено 08.08.2026.
# reportjson (50 ₽) их не отдаёт: там только счётчик регистраций без записей,
# а характеристик машины нет вовсе. Две самые заметные секции у конкурентов
# стоят нам две копейки, и не добирать их было бы странно.

_PERSON = {"Natural": "Физическое лицо", "Legal": "Юридическое лицо"}


def _pass_num(v: Any) -> str:
    """«1995.0» → «1995». Источник отдаёт числа строками с хвостом."""
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def enrich(vin: str) -> dict[str, Any]:
    """Паспорт автомобиля и история владения.

    Возвращает пустой словарь при любой беде: секции просто не покажутся, а
    отчёт останется. Ронять выдачу из-за дополнения нельзя — за неё заплачено.
    """
    try:
        got = apipoint.call("gibddhistory", {"vin": vin})
    except Exception as exc:  # noqa: BLE001
        print(f"[enrich] gibddhistory: {type(exc).__name__}: {exc}", flush=True)
        return {}
    inner = (got.get("result") or {}).get("gibddhistory") or {}
    # result внутри — СТРОКА с JSON, а не объект. Разбираем отдельно, иначе
    # получим «строка не поддерживает .get» на ровном месте.
    raw = inner.get("result")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(raw, dict):
        return {}
    rr = raw.get("RequestResult") or {}
    veh = rr.get("vehicle") or {}

    as_of = None
    for key in ("requestTime",):
        t = str(raw.get(key) or inner.get(key) or "")
        if len(t) >= 10:
            try:
                d, m, y = t[:10].split(".")
                as_of = date(int(y), int(m), int(d))
            except Exception:  # noqa: BLE001
                pass

    passport = {
        "model": (veh.get("model") or "").strip(),
        "year": _pass_num(veh.get("year")),
        "color": (veh.get("color") or "").strip().capitalize(),
        "volume": _pass_num(veh.get("engineVolume")),
        "power_hp": _pass_num(veh.get("powerHp")),
        "engine_no": (veh.get("engineNumber") or "").strip(),
        "body_no": (veh.get("bodyNumber") or "").strip(),
        "category": (veh.get("category") or "").strip(),
        "as_of": as_of.isoformat() if as_of else "",
    }

    periods = ((rr.get("ownershipPeriods") or {}).get("ownershipPeriod")) or []
    if isinstance(periods, dict):        # один период приходит объектом, не списком
        periods = [periods]
    owners = []
    for p in periods:
        if not isinstance(p, dict):
            continue
        owners.append({
            "kind": _PERSON.get(str(p.get("simplePersonType") or ""), "Не указано"),
            "from": str(p.get("from") or ""),
            "to": str(p.get("to") or ""),
        })
    # Источник отдаёт периоды в произвольном порядке — сортируем по дате начала,
    # иначе таблица «периоды владения» читается как случайный набор строк.
    owners.sort(key=lambda o: o.get("from") or "")
    return {"passport": {k: v for k, v in passport.items() if v},
            "owners": owners}


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
