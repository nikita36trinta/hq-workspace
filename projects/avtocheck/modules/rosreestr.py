"""
Проверка объекта недвижимости через NewDB `rosreestr`.

По АДРЕСУ метод возвращает: кадастровый номер, площадь, назначение,
кадастровую стоимость, права (собственники + доли) и обременения (аресты,
ипотеки). Это позволяет:
  1. превратить текстовый адрес в кадастровый номер (и регион для ФССП);
  2. проверить сам объект: доли/многособственность и обременения.

Метод принимает ТОЛЬКО address. Для ввода кадастром объектную проверку не
делаем — регион берём из первых двух цифр кадастра.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from modules import newdb


def _address_from_kadastr(kad: str) -> str:
    """Реверс: кадастровый номер → адрес через DaData (у нас уже подключён).
    NewDB rosreestr принимает только адрес, поэтому по кадастру сначала
    находим адрес, а по нему — объект."""
    key = os.getenv("DADATA_API_KEY", "").strip()
    if not key:
        return ""
    try:
        import httpx
        r = httpx.post(
            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address",
            headers={"Content-Type": "application/json", "Authorization": f"Token {key}"},
            json={"query": kad.strip(), "count": 1}, timeout=10.0,
        )
        sug = r.json().get("suggestions", [])
        return (sug[0].get("value") or "") if sug else ""
    except Exception:
        return ""


@dataclass
class ObjectResult:
    status: str            # "found" | "not_found" | "not_checked"
    detail: str
    items: list[dict[str, Any]] = field(default_factory=list)
    cadnum: str = ""
    region: str = ""
    serious: bool = False  # найдено серьёзное обременение (арест/запрет/изъятие)
    # Факты о правах отдельными полями, а не только внутри detail. Пакет к сделке
    # решает по ним, что запрашивать у продавца (согласие супруга, отказы
    # сособственников), и разбирать ради этого нашу же прозу регуляркой — способ
    # сломать всё при первой правке формулировки.
    shared: bool = False          # долевая/совместная собственность
    owners_n: int = 0             # сколько записей о правах
    parts: list[str] = field(default_factory=list)   # доли, если раскрыты: ["1/2", "1/2"]
    reg_date: str = ""            # дата регистрации текущего права
    # ПОСТОЯННАЯ неудача (объекта по такому адресу/кадастру в ЕГРН нет) vs ВРЕМЕННАЯ
    # (сервис не ответил). Ретраить имеет смысл только временную — иначе бесконечно
    # платим за один и тот же безнадёжный запрос (инцидент 2026-07-25: ~100 запросов
    # за ночь на кривой адрес «Город Калуга ул дружба д 10 кв 13 5 этаж»).
    permanent: bool = False


# Справочник типов обременений Росреестра (первые 6 цифр кода из ЕГРН).
# Нужен, когда сервис не вернул текстовое описание (encumbranceTypeDesc пуст).
_ENC_TYPES: dict[str, str] = {
    "022001": "Ипотека",
    "022002": "Ипотека в силу закона",
    "022006": "Аренда",
    "022007": "Безвозмездное пользование",
    "022010": "Доверительное управление",
    "022012": "Рента / пожизненное содержание",
    "022013": "Сервитут",
    "022016": "Арест",
    "022017": "Запрещение регистрации / запрет сделок",
    "022018": "Решение об изъятии объекта",
    "022098": "Ограничение (обременение) прав",
    "022099": "Иное ограничение (обременение)",
}
# Серьёзные обременения — блокируют/ставят под угрозу переход права → высокий риск.
_ENC_SERIOUS: set[str] = {"022016", "022017", "022018"}


def _enc_code(e: dict[str, Any]) -> str:
    """Первые 6 цифр кода типа обременения (напр. '022006000000' → '022006')."""
    return str(e.get("type") or "")[:6]


def _iso_date(value: Any) -> str:
    """Дату права — к виду ГГГГ-ММ-ДД. Источники дают разный формат: NewDB
    «22.08.2017», api-assist «2017-08-22». Сортировать их вперемешку как строки
    нельзя — самой свежей окажется случайная."""
    s = str(value or "").strip()[:10]
    if not s:
        return ""
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    if len(s) == 10 and s[2] == "." and s[5] == ".":
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
    return ""


def _enc_label(e: dict[str, Any]) -> str:
    """Человекочитаемая метка обременения: «Аренда (зарег. 17.12.2025)».
    Берём текст сервиса; если пуст — расшифровываем код по справочнику."""
    desc = (e.get("encumbranceTypeDesc") or e.get("typeDesc") or "").strip()
    if not desc:
        code = _enc_code(e)
        desc = _ENC_TYPES.get(code) or (f"ограничение (код {code})" if code else "обременение")
    date = (e.get("startDate") or e.get("regDate") or e.get("encumbranceDate") or "").strip()
    return f"{desc} (зарег. {date})" if date else desc


_KADASTR_RE = re.compile(r"^\s*(\d{2}):\d{1,2}:\d{1,7}:\d{1,7}\s*$")


def is_kadastr(object_ref: str) -> bool:
    return bool(_KADASTR_RE.match(object_ref or ""))


# Кадастровый номер набирают руками, и опечатка превращает его в «адрес»,
# который потом не находится ни в ЕГРН, ни в НСПД. В сегменте «объект не найден»
# (400 заявок с 24.07) такие вводы — четверть. Проверено на боевых примерах:
# «50 :30 :0050117 :457» и «50:21:0080105:25126-50» после чистки находятся оба.
#
# Правило намеренно осторожное: цифры не меняем и порядок не трогаем, результат
# обязан пройти _KADASTR_RE — иначе возвращаем ввод как есть. Ошибиться можно
# только в сторону «не смогли починить», но не в сторону чужого объекта.
# Хвост после кадастрового номера — это номер записи регистрации, а не другой объект:
# «54:35:033045:532-54/163/2026-5» и «50:21:0080105:25126-50». Сам кадастр слева от
# дефиса рабочий, поэтому хвост срезаем целиком, а не только цифровой.
_KAD_TAIL_RE = re.compile(r"^(\d{2}:\d{1,2}:\d{1,7}:\d{1,7})-\S+$")

# Номер внутри произвольного текста: «Дом-47:14:0903004:424(82,9кв.м)», «егрн 77:07:...».
# Разделителем человек ставит что угодно, поэтому принимаем весь набор. Границы по
# цифрам обязательны — иначе шаблон откусывал бы куски более длинных числовых строк.
_KAD_EMBED_RE = re.compile(r"(?<!\d)(\d{2})[:;.\-](\d{1,2})[:;.\-](\d{1,7})[:;.\-](\d{1,7})(?!\d)")

# Единый 14-значный вид ГКН: РР ММ КККККММ ННН → 2+2+7+3. «50210010106220» это
# 50:21:0010106:220, проверено в НСПД. Берём только ровно 14 цифр: на 13 или 15
# разбиение неоднозначно, а угадывать разбиение — это выдать чужой объект.
_KAD_FLAT_RE = re.compile(r"^\d{14}$")


def normalize_cadastre(object_ref: str) -> str:
    """Починить опечатки в кадастровом номере. Не кадастр — вернуть как есть.

    Порядок правил не случаен: сначала пробуем вытащить номер целиком из текста,
    и только потом чиним разделители. Иначе «Дом-47:14:0903004:424 Участок-47:14:
    0903004:347» после замены дефисов на двоеточия превратился бы в кашу, из
    которой мы бы «починили» первый попавшийся кусок.
    """
    raw = (object_ref or "").strip()
    if not raw or is_kadastr(raw):
        return raw
    # Юникодные тире и неразрывные пробелы: приходят из выписок и из Word.
    # «77:04:0003001:5190‑77/072/2026‑5» ломался только на U+2011 в хвосте.
    t = raw.translate(str.maketrans({"‐": "-", "‑": "-", "‒": "-",
                                     "–": "-", "—": "-", "−": "-",
                                     " ": " ", " ": " "}))
    t = re.sub(r"\s*:\s*", ":", t.strip(" .,;"))     # пробелы вокруг двоеточий
    t = re.sub(r"::+", ":", t)                        # 63::01 → 63:01
    if is_kadastr(t):
        return t
    m = _KAD_TAIL_RE.match(t)                         # 50:21:...:25126-50 → без хвоста
    if m:
        return m.group(1)
    # Номер внутри текста — но ТОЛЬКО если он там один. Два разных номера это дом
    # и участок в одной заявке: человек оплатил проверку обоих, и молча взять
    # первый значит выдать половину услуги, сделав вид, что это целое.
    found = {":".join(g) for g in _KAD_EMBED_RE.findall(t)}
    if len(found) == 1:
        one = found.pop()
        if is_kadastr(one):
            return one
    cand = re.sub(r"::+", ":", re.sub(r"[-./,;]", ":", t))  # прочие разделители
    if is_kadastr(cand):
        return cand
    # Кириллицу вместо цифр («…:З9б» вместо «…:396») намеренно НЕ чиним: замена
    # даёт синтаксически валидный номер, а значит существующий, но чужой объект.
    # Такой ввод честнее вернуть как есть и спросить у человека.
    flat = re.sub(r"\s+", "", t)                       # 14 цифр подряд → 2+2+7+3
    if _KAD_FLAT_RE.match(flat):
        split = f"{flat[:2]}:{flat[2:4]}:{flat[4:11]}:{flat[11:]}"
        if is_kadastr(split):
            return split
    return raw


# Три группы вместо четырёх: человек не поставил одно двоеточие внутри номера,
# «86:130501002:333» вместо «86:13:0501002:333». Валидатор такой ввод отвергает,
# объект не ищется, оплаченная проверка падает — 13 таких заявок за месяц.
_KAD_GLUED_RE = re.compile(r"^(\d{2}):(\d{7,10}):(\d{1,7})$")


def _human_reg_date(iso: str) -> str:
    """ГГГГ-ММ-ДД → ДД.ММ.ГГГГ. В отчёте для человека ISO выглядит отладочным выводом."""
    s = str(iso or "").strip()[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}.{s[5:7]}.{s[0:4]}"
    return s


def _right_is_fresh(iso: str, years: float = 3.0) -> bool:
    """Право моложе трёх лет — срок, в который сделку прежнего собственника ещё
    можно оспорить при его банкротстве (ст. 61.2 ФЗ-127)."""
    s = str(iso or "").strip()[:10]
    if len(s) != 10:
        return False
    try:
        from datetime import datetime
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return False
    return (datetime.utcnow() - d).days < years * 365.25


def glued_cadastre_variants(object_ref: str) -> list[str]:
    """Кандидаты для номера со слипшимися районом и кварталом.

    Возвращает ВАРИАНТЫ, а не ответ: разбить «130501002» можно и как 1+30501002,
    и как 13+0501002. Оба синтаксически валидны, но существует обычно один —
    поэтому выбирать обязан тот, кто может спросить реестр, а не эта функция.
    Угадывать вслепую нельзя: чужой объект хуже, чем отказ.
    """
    m = _KAD_GLUED_RE.match((object_ref or "").strip())
    if not m:
        return []
    region, middle, obj = m.groups()
    out: list[str] = []
    for d in (2, 1):                       # район чаще двузначный — пробуем его первым
        district, quarter = middle[:d], middle[d:]
        if not (5 <= len(quarter) <= 7):
            continue
        cand = f"{region}:{district}:{quarter}:{obj}"
        if is_kadastr(cand):
            out.append(cand)
    return out


def region_from_kadastr(object_ref: str) -> str:
    """Код региона из кадастра. Единственный потребитель — ФССП, поэтому берём
    её справочник расхождений (кадастровый округ ≠ код субъекта), а не свой:
    иначе объектная ветка отдавала бы «95» и запрос отбивался с HTTP 400."""
    from modules.fssp import region_from_kadastr as _fssp_region
    return _fssp_region(object_ref)


# кэш нормализации адреса, чтобы не платить DaData дважды за один и тот же ввод
_CLEAN_CACHE: dict[str, tuple[str, str, str]] = {}


def _dadata_clean(address: str) -> tuple[str, str, str]:
    """Нормализовать «человеческий» адрес через DaData Clean.

    Возвращает (нормализованный_адрес, кадастр_квартиры, код_региона) — любое
    может быть пустым. Стоит ~0.2₽, но экономит платный пустой запрос в Росреестр
    и спасает кривой ввод.

    Код региона берём из region_kladr_id: DaData знает субъект даже там, где в
    адресе назван только город («г Краснодар» → 23), а нумерация КЛАДР совпадает
    с кодами ФССП — в отличие от кадастровых округов, где Чечня 95 против 20.
    Это точнее и таблицы названий, и любых догадок по тексту.
    """
    import os
    import httpx
    key = os.getenv("DADATA_API_KEY", "").strip()
    sec = os.getenv("DADATA_SECRET_KEY", "").strip()
    if not (key and sec) or not address.strip():
        return "", "", ""
    ck = " ".join(address.lower().split())
    if ck in _CLEAN_CACHE:
        return _CLEAN_CACHE[ck]
    out = ("", "", "")
    try:
        r = httpx.post(
            "https://cleaner.dadata.ru/api/v1/clean/address", json=[address],
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": f"Token {key}", "X-Secret": sec},
            timeout=15.0,
        )
        if r.status_code == 200:
            rows = r.json() or []
            o = rows[0] if rows and isinstance(rows[0], dict) else {}
            kladr = str(o.get("region_kladr_id") or "")
            out = (str(o.get("result") or ""), str(o.get("flat_cadnum") or ""),
                   kladr[:2] if len(kladr) >= 2 and kladr[:2].isdigit() else "")
            if len(_CLEAN_CACHE) > 5000:
                _CLEAN_CACHE.clear()
            _CLEAN_CACHE[ck] = out
    except Exception:  # noqa: BLE001 — нормализация best-effort
        pass
    return out


# Слова, которыми реестр называет ЗЕМЛЮ. Если бесплатный НСПД говорит «участок»,
# а платный поставщик — «здание», прав НСПД: он берёт тип из карточки объекта,
# а не из поля назначения, которое у участков часто заполнено мусором.
_LAND_WORDS = ("земельный участок", "участок", "земли")
# Слова, при которых стоит переспросить: поставщик назвал объект СТРОЕНИЕМ.
_BUILDING_RE = re.compile(r"здани|сооружени|строени|дом\b", re.I)


def _kind_word(cadnum: str, provider_word: str) -> str:
    """Чем назвать объект в тексте отчёта.

    07.08 три отчёта подряд назвали земельный участок «зданием»: NewDB отдал
    purpose_text='Здание' для участков в Псковской и Саратовской областях, а мы
    печатали это слово дословно. Клиент читает «право пользования зданием» про
    свои шесть соток и справедливо считает отчёт липовым.

    Переспрашиваем у НСПД — он бесплатный, ищет по кадастру и уже стоит у нас в
    превью. Молчит или кадастра нет — оставляем слово поставщика: выдумывать
    своё нельзя, а без слова строка тоже читается.

    Правим ТОЛЬКО землю, и только когда поставщик назвал объект строением. У
    помещений его слово — это назначение («жилое», «нежилое»); оно верное и
    полезное, подменять его на «Квартира» значит терять сведения ради косметики.
    """
    word = str(provider_word or "").strip()
    # Спорить не о чем: поставщик и так не назвал объект строением.
    if not cadnum or not word or not _BUILDING_RE.search(word):
        return word
    try:
        from modules import nspd
        o = nspd.object_by_cadastre(cadnum, timeout=8.0) or {}
    except Exception:  # noqa: BLE001 — уточнение не имеет права ронять платную проверку
        return word
    if not o:
        return word
    kind = str(o.get("type") or "").strip().lower()
    if o.get("is_land") or kind in _LAND_WORDS:
        return "земельный участок"
    return word


def check_object(object_ref: str, timeout: float = 300.0, attempts: int = 1) -> ObjectResult:
    """Адрес → кадастр + проверка объекта (права/обременения).

    ОДНА попытка и 5 минут ожидания вместо трёх попыток по 70с. Причина: у NewDB
    submit тарифицируется сразу, а источник отвечает без верхней границы времени —
    повтор не ускоряет ответ, он покупает вторую очередь к тому же тормозящему
    источнику. 25.07 объект 77:05:0005005:4879 оплатили трижды, и все три запроса
    в итоге ВЫПОЛНИЛИСЬ — просто позже, чем мы соглашались ждать.

    Если не успели и за 5 минут — requestId уже записан в newdb_pending.jsonl,
    результат забирается позже бесплатно (newdb.fetch)."""
    ref = (object_ref or "").strip()
    if not ref:
        return ObjectResult("not_checked", "Объект не указан.")

    # NewDB rosreestr принимает в поле address И адрес, И кадастровый номер.
    kad_region = region_from_kadastr(ref) if is_kadastr(ref) else ""

    # Клиенты вводят адрес как попало («Город Калуга ул дружба д 10 кв 13 5 этаж») — сырой
    # такой Росреестр не находит, а мы платим за пустой ответ. Нормализуем через DaData
    # (дёшево, ~0.2₽) и получаем заодно кадастр квартиры. Кандидаты пробуем по очереди.
    # Порядок кандидатов = от самого точного к самому грубому. Сырой ввод пробуем
    # ТОЛЬКО если DaData не дала нормализованную форму — иначе это заведомо лишний
    # платный запрос (инцидент 2026-07-25: один и тот же кривой адрес ушёл в Росреестр
    # трижды подряд, хотя нормализованный вариант был известен сразу).
    candidates: list[str] = []
    dd_region = ""
    if not is_kadastr(ref):
        try:
            cleaned, flat_cad, dd_region = _dadata_clean(ref)
            if flat_cad:
                candidates.append(flat_cad)         # точный кадастр квартиры — лучший вход
            if cleaned and cleaned.lower() != ref.lower():
                candidates.append(cleaned)          # нормализованный адрес — запасной
        except Exception:  # noqa: BLE001 — нормализация необязательна
            pass
    if not candidates:
        candidates.append(ref)

    rows = None
    last_err = ""
    for i, cand in enumerate(candidates):
        try:
            # Полный набор ретраев тратим только на первого (самого точного) кандидата;
            # остальные пробуем одной попыткой — их задача перекрыть кривой ввод,
            # а не переждать сбой источника.
            rows = newdb.call("rosreestr", {"address": cand},
                              timeout=timeout, attempts=(attempts if i == 0 else 1))
        except newdb.NewDBError as e:
            # Источник НЕ ответил (таймаут/5xx). Другие кандидаты уйдут в тот же
            # таймаут — перебирать их бессмысленно и платно. Останавливаемся.
            last_err = str(e)
            rows = None
            break
        if rows:
            if not is_kadastr(ref) and is_kadastr(cand):
                kad_region = kad_region or region_from_kadastr(cand)
            break              # нашли — дальше не платим
        # rows пусто = сервис ОТВЕТИЛ «не найдено» → есть смысл попробовать следующего
    # ЗАПАСНОЙ ИСТОЧНИК. Пробуем, когда основной не ответил ИЛИ ответил «не найдено»:
    # второе не менее важно первого. Отчёт 2176aea372e0 (Калуга) сутки числился
    # «объекта нет в ЕГРН» после 131 пустого ответа NewDB — на деле его поиск
    # спотыкался о хвост «5 этаж» в адресе. api-assist по той же строке нашёл
    # объект, и на нём висел ЗАПРЕТ РЕГИСТРАЦИИ. Пустой ответ одного источника
    # больше не считаем приговором.
    if not rows:
        try:
            from modules import apiassist
            if apiassist.enabled():
                # Отдаём ТЕ ЖЕ кандидаты, что и основному источнику: сырой ввод
                # («Город Калуга ул дружба д 10 кв 13 5 этаж») не разбирает никто,
                # нормализованный DaData — разбирают оба. Плюс сырой последним,
                # если DaData ничего не дала.
                alt_refs = list(candidates)
                if ref not in alt_refs:
                    alt_refs.append(ref)
                alt: list[dict[str, Any]] = []
                for cand in alt_refs:
                    alt = apiassist.object_rows(cand, timeout=min(timeout, 90.0))
                    if alt:
                        break
                if alt:
                    print(f"[fallback] api-assist нашёл объект, которого не дал NewDB: "
                          f"{ref[:60]}", flush=True)
                    # Спасённая проверка — это оплаченный отчёт, который иначе ушёл бы
                    # в «объект не найден». Пишем в журнал, иначе ценность запасного
                    # источника видна только в логах контейнера.
                    try:
                        import json as _json
                        from datetime import datetime as _dt
                        from pathlib import Path as _P
                        _p = _P(__file__).resolve().parent.parent / "data" / "apiassist.jsonl"
                        with open(_p, "a", encoding="utf-8") as _f:
                            _f.write(_json.dumps({"ts": _dt.utcnow().isoformat() + "Z",
                                                  "event": "rescue_paid"}, ensure_ascii=False) + "\n")
                    except Exception:  # noqa: BLE001
                        pass
                    rows, last_err = alt, ""
                    cad_alt = str(alt[0].get("cadNumber") or "")
                    if not is_kadastr(ref) and is_kadastr(cad_alt):
                        kad_region = kad_region or region_from_kadastr(cad_alt)
        except Exception as e:  # noqa: BLE001 — запасной источник не имеет права ломать основной путь
            print(f"[fallback] api-assist не помог: {type(e).__name__}: {e}", flush=True)

    if rows is None and last_err:
        return ObjectResult(
            "not_checked", f"Росреестр: сервис недоступен ({last_err}).",
            cadnum=(object_ref.strip() if is_kadastr(ref) else ""),
            region=kad_region or dd_region,
        )
    if not rows:
        # Сервис ОТВЕТИЛ (status 200, data пустой) — объекта нет. Это ПОСТОЯННО:
        # ретрай ничего не изменит, нужен другой адрес/кадастр от клиента.
        return ObjectResult(
            "not_checked",
            "Объект по этому адресу в ЕГРН не найден — проверьте адрес "
            "(город, улица, дом, квартира) или введите кадастровый номер.",
            region=kad_region or dd_region, permanent=True,
        )

    obj = rows[0] if isinstance(rows[0], dict) else {}
    cadnum = str(obj.get("cadNumber") or "")
    region = region_from_kadastr(cadnum) or kad_region or dd_region
    area = obj.get("area")
    purpose = _kind_word(cadnum, obj.get("purpose_text") or obj.get("objType_text") or "")
    encumbrances = obj.get("encumbrances") or []
    rights = obj.get("rights") or []

    # доли собственности
    parts = [str(r.get("part") or "") for r in rights if isinstance(r, dict)]
    shared = any(p and p not in ("1", "1/1", "") for p in parts)
    # Запасной источник долей не отдаёт — там долевая видна только по типу права
    # («Общая долевая собственность»). Без этой строки объект с одной записью
    # такого типа прошёл бы как единоличный, и клиент не узнал бы, что нужны
    # согласия сособственников.
    shared = shared or any(
        "долев" in str(r.get("rightTypeDesc") or "").lower()
        for r in rights if isinstance(r, dict)
    )
    owners_n = len([r for r in rights if isinstance(r, dict)])
    # Самая свежая дата регистрации права: по ней пакет к сделке решает, спрашивать
    # ли документ-основание (право моложе трёх лет — срок оспаривания не вышел).
    # NewDB (основной источник) называет поле rightRegDate, api-assist после
    # to_newdb_shape — regDate. Читали только второе, поэтому у объектов из NewDB
    # дата права терялась ВСЕГДА, и правило пакета «право моложе трёх лет» не
    # срабатывало ни разу.
    reg_dates = sorted(_iso_date(r.get("rightRegDate") or r.get("regDate") or r.get("date") or "")
                       for r in rights if isinstance(r, dict))
    reg_dates = [d for d in reg_dates if d]
    reg_date = reg_dates[-1] if reg_dates else ""

    items: list[dict[str, Any]] = []
    flags: list[str] = []
    serious_enc = False   # арест/запрет/изъятие — высокий риск (не просто аренда/ипотека)

    if encumbrances:
        labels = []
        for e in encumbrances:
            if not isinstance(e, dict):
                continue
            lbl = _enc_label(e)
            labels.append(lbl)
            if _enc_code(e) in _ENC_SERIOUS:
                serious_enc = True
            items.append({"type": lbl,
                          "date": e.get("startDate") or e.get("regDate") or ""})
        if len(labels) == 1:
            flags.append(f"обременение — {labels[0]}")
        else:
            flags.append(f"обременения ({len(labels)}): " + "; ".join(labels))

    if shared or owners_n > 1:
        known = [p for p in parts if p]
        if known:
            who = "доли: " + ", ".join(known)
        elif owners_n > 1:
            who = f"собственников: {owners_n}, доли в выписке не раскрыты"
        else:
            who = "доли в выписке не раскрыты"
        flags.append(f"долевая собственность ({who})")

    base = []
    if cadnum:
        base.append(f"кадастровый № {cadnum}")
    if area:
        base.append(f"площадь {area} м²")
    if purpose:
        base.append(str(purpose).lower())
    # Дата регистрации права — сильный сигнал для покупателя: пока не прошло три
    # года, сделку может оспорить кредитор или управляющий прежнего собственника
    # (ст. 61.2 ФЗ-127). Раньше она никуда не выводилась, и заключение о ней
    # молчало, хотя данные у нас были.
    if reg_date:
        base.append(f"право зарегистрировано {_human_reg_date(reg_date)}")
    obj_line = "Объект найден в ЕГРН: " + ", ".join(base) + "." if base else "Объект найден в ЕГРН."
    if _right_is_fresh(reg_date):
        obj_line += (" Право моложе трёх лет — срок оспаривания сделок прежнего "
                     "собственника ещё не истёк.")

    if flags:
        # «Требует внимания» собираем из РЕАЛЬНЫХ флагов, а не хардкодом.
        tail = []
        if encumbrances:
            tail.append("обременения ограничивают право покупателя и могут стать основанием "
                        "оспорить сделку — их нужно снять до подписания договора")
        if shared or owners_n > 1:
            tail.append("долевая собственность — потребуются нотариальные согласия или отказы "
                        "всех сособственников")
        detail = obj_line + " ⚠️ " + "; ".join(flags) + "."
        if tail:
            detail += " Требует внимания: " + "; ".join(tail) + "."
        return ObjectResult(
            "found", detail, items=items, cadnum=cadnum, region=region,
            serious=serious_enc,
            shared=bool(shared or owners_n > 1), owners_n=owners_n,
            parts=[p for p in parts if p], reg_date=reg_date,
        )
    # Флагов нет — но данные о правах есть, и терять их нельзя: по дате регистрации
    # права пакет к сделке решает, требовать ли документ-основание (право моложе
    # трёх лет — срок оспаривания не вышел). Раньше эта ветка возвращала только
    # кадастр и регион, поэтому у ЧИСТЫХ объектов — а это большинство — дата права
    # не доезжала никуда, и правило не срабатывало ни разу.
    return ObjectResult(
        "not_found",
        obj_line + " Обременений и арестов не зарегистрировано, собственность единоличная.",
        cadnum=cadnum, region=region,
        owners_n=owners_n, parts=[p for p in parts if p], reg_date=reg_date,
    )
