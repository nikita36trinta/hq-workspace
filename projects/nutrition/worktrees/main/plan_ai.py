"""LLM-генерация персонального плана питания (структурированный JSON).

Учитывает ВСЕ сигналы квиза: цель, норму КБЖУ, ограничения, любимое, число
приёмов, время на готовку, барьеры. Продукты — доступные в РФ. При сбое —
fallback на статичный банк (plan.week).

Выход (dict): {"cal","P","F","C","goal","days":[{day,meals:[{slot,name,kcal,p,f,c,ingredients[],steps[]}]}],
               "shopping":[{cat,items[]}], "tips":[...]}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from pathlib import Path

import plan as _plan

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TEXT_MODEL = os.getenv("NB_TEXT_MODEL", "google/gemini-2.5-flash")

GOAL_RU = {"lose": "снижение веса", "keep": "поддержание формы",
           "gain": "набор мышечной массы", "health": "наладить питание, энергия"}
ACT_RU = {"sed": "почти не двигается", "light": "1–2 трен/нед", "mod": "3–4 трен/нед", "high": "5+ трен/нед"}
MEALS_RU = {"2": "2 приёма пищи", "3": "3 приёма пищи", "3s": "3 приёма + перекусы",
            "if": "интервальное голодание (окно 8ч)"}
COOK_RU = {"q15": "до 15 минут (простое)", "q30": "20–30 минут", "any": "любит готовить, можно сложнее",
           "none": "без готовки (готовое/доставка)"}
DIET_RU = {"all": "ест всё", "nomeat": "без мяса (вегетарианец)", "nolact": "без лактозы",
           "nogluten": "без глютена", "nofish": "не ест рыбу и морепродукты", "nuts": "аллергия на орехи"}
FAV_RU = {"meat": "мясо и птица", "fish": "рыба", "veg": "овощи и салаты", "grain": "крупы и паста",
          "dairy": "молочное", "sweet": "сладкое в меру"}
BARR_RU = {"time": "нет времени готовить", "sweet": "срывы на сладкое", "what": "не знает что есть",
           "run": "ест на бегу", "count": "сложно считать калории", "yoyo": "вес возвращается"}


def _key() -> str:
    if os.getenv("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    # Удобство для разработки: подобрать ключ из .env пайплайнов, когда запуск
    # идёт из воркспейса. В контейнере у /app/plan_ai.py всего два родителя,
    # поэтому обращение к parents[2..7] вслепую бросало IndexError — и «ключ не
    # настроен» превращалось в падение на пути выдачи ОПЛАЧЕННОГО плана, где
    # исключение молча гасится, а клиент остаётся без того, за что заплатил.
    parents = Path(__file__).resolve().parents
    for up in [parents[i] for i in range(2, min(8, len(parents)))]:
        env = up / ".hq" / "pipelines" / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip()
    return ""


def _urlopen(req, timeout):
    """openurl с прокси LLM_PROXY (на RU-проде OpenRouter гео-блокирует прямой IP)."""
    proxy = os.getenv("LLM_PROXY", "").strip()
    if proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _labels(codes, mapping):
    return ", ".join(mapping.get(c, c) for c in (codes or [])) or "—"


_CATALOG = None


def _catalog() -> list:
    """Каталог канон-блюд (dishes.json) — ограничивает словарь → фото гарантированно есть."""
    global _CATALOG
    if _CATALOG is None:
        try:
            _CATALOG = json.loads((Path(__file__).parent / "dishes.json").read_text(encoding="utf-8")).get("dishes", [])
        except Exception:  # noqa: BLE001
            _CATALOG = []
    return _CATALOG


# суффиксы существительных + прилагательных (для стемминга рус-слов)
_SUF = ("ами", "ями", "ыми", "ими", "ого", "его", "ому", "ему", "ов", "ев", "ой", "ей",
        "ый", "ий", "ая", "яя", "ое", "ее", "ые", "ие", "ом", "ем", "ым", "им", "ах", "ях",
        "ы", "и", "а", "я", "у", "ю", "е", "о", "ь", "й")


def _stem(w: str) -> str:
    """Грубый стем рус-слова — «рыба»↔«рыбной», «курица»↔«куриный», «грибы»↔«грибной»."""
    w = w.strip().lower().replace("ё", "е")
    for s in _SUF:
        if w.endswith(s) and len(w) - len(s) >= 3:
            return w[:-len(s)]
    return w


def _words(text: str) -> list[str]:
    out, cur = [], []
    for ch in (text or "").lower():
        if "а" <= ch <= "я" or ch == "ё":
            cur.append(ch)
        elif cur:
            out.append("".join(cur)); cur = []
    if cur:
        out.append("".join(cur))
    return out


def _term_hits(term_stem: str, text: str) -> bool:
    """Есть ли в тексте слово, чей стем совпадает с term_stem (по общему префиксу ≥4 —
    ловит чередование ц/ч/н: куриц↔курин↔курич)."""
    if not term_stem:
        return False
    for w in _words(text):
        ws = _stem(w)
        if ws == term_stem or ws.startswith(term_stem) or term_stem.startswith(ws):
            if min(len(ws), len(term_stem)) >= 3:
                return True
        if len(term_stem) >= 4 and len(ws) >= 4 and ws[:4] == term_stem[:4]:
            return True
    return False


def _excluded_terms(quiz: dict) -> list[str]:
    """Стемы продуктов, которые пользователь не ест (свободный список через запятую)."""
    raw = quiz.get("exclude") or ""
    return [_stem(t) for t in raw.replace(";", ",").split(",") if t.strip()]


# Аллергены/диет-коды → слова-маркеры в названии/ингредиентах. dishes.json не хранит флаги
# gluten/nuts, поэтому фильтруем по ключевым словам (лучше перебдеть, чем накормить аллергена).
_ALLERGEN_WORDS = {
    "nuts": ["орех", "арахис", "миндал", "фундук", "кешью", "фисташ", "пекан", "грецк",
             "гранол", "мюсли", "нутелл", "пралине", "марципан", "урбеч", "тахин", "кунжут"],
    "nogluten": ["хлеб", "тост", "паст", "макарон", "спагетти", "лапш", "булгур", "кускус",
                 "манк", "блин", "оладь", "сырник", "панир", "сухар", "пельмен", "вареник",
                 "лаваш", "багет", "батон", "круассан", "печень", "пирог", "пицц", "бургер",
                 "вафл", "мюсли", "гранол", "овсян", "перловк", "пшениц", "пшённ", "ячмен",
                 "манты", "хинкал", "чебурек", "блинчик", "тортилья", "кекс", "маффин", "сэндвич",
                 # Дыра, найденная на живом экране: человек просил «без глютена» и получал
                 # БУТЕРБРОД. Фильтр знал «тост» и «сэндвич», но не «бутерброд», «гренки»,
                 # «лазанья», «кесадилья» и «пита» — всё это тесто.
                 # «пит» без уточнения брать нельзя: под него попадает «Йогурт питьевой».
                 "бутерброд", "гренк", "лазань", "кесадиль", "в пите", "пита", "наггетс",
                 "шаурм", "хачапури", "самса", "лепёшк", "пряник", "крутон", "тортиль"],
    "nomeat": ["курин", "куриц", "говяд", "свин", "телятин", "баранин", "индейк", "мясн",
               "фарш", "котлет", "бекон", "колбас", "сосиск", "ветчин", "буженин", "стейк"],
    "nofish": ["рыб", "лосос", "тунец", "форел", "сельд", "скумбри", "треск", "минтай",
               "креветк", "кальмар", "мидии", "морепродукт", "икр", "краб", "суши", "роллы"],
    "nolact": ["молок", "творог", "сыр", "йогурт", "кефир", "сметан", "сливк", "масл сливоч",
               "ряженк", "брынз", "моцарелл", "маскарпоне", "сырник"],
}


def _forbidden_words(quiz: dict, skip: tuple[str, ...] = ()) -> list[str]:
    """Слова-маркеры, запрещённые диетой/аллергиями пользователя.

    skip — коды, для которых словарь НЕ применяем. Нужен для каталога: там у
    блюда есть точный флаг (meat), а словарь слишком груб — «котлет» рубит
    «Картофельные котлеты», «стейк» рубит «Стейк из лосося». Для блюд, которые
    придумала модель, флагов нет, и словарь остаётся единственной защитой."""
    diet = set(quiz.get("diet") or [])
    words: list[str] = []
    for code in diet:
        if code in skip:
            continue
        words += _ALLERGEN_WORDS.get(code, [])
    return words


# Растительное «молоко» и «сыр» лактозы не содержат, а словарь ловит их по корню:
# «Каша киноа на растительном молоке» вылетала у людей без лактозы — то есть
# ровно то блюдо, которое для них и добавлено. Сначала вырезаем эти сочетания,
# потом уже ищем запрещённые корни.
_LACT_OK = re.compile(
    r'(растительн\w*|кокосов\w*|миндальн\w*|сое\w*|соев\w*|овсян\w*|рисов\w*|гречнев\w*|'
    r'кешью|фундучн\w*|конопля\w*|веган\w*)[\s-]+(молок\w*|сливк\w*|сыр\w*|йогурт\w*)'
    r'|молок\w*[\s-]+(растительн\w*|кокосов\w*|миндальн\w*|сое\w*|соев\w*|овсян\w*|рисов\w*)'
    r'|тофу', re.I)


_LACT_WORDS = frozenset(_ALLERGEN_WORDS.get("nolact", []))

# «паст» в списке глютена — про макароны, но ловит ТОМАТНУЮ ПАСТУ, а она есть почти в
# каждом супе и рагу: на живом ответе модели из-за неё браковался весь план, и человек
# без глютена уезжал в банк-фолбэк без рецептов. Пастила и пастернак — то же самое.
_GLU_OK = re.compile(
    r'(томатн\w*|арахисов\w*|орехов\w*|кунжутн\w*|шоколадн\w*|миндальн\w*|фисташков\w*|'
    r'кокосов\w*|карри|мисо)[\s-]+паст\w*'
    r'|паст\w*[\s-]+(томатн\w*|из томат\w*)'
    r'|пастил\w*|пастернак\w*', re.I)

_GLU_WORDS = frozenset(_ALLERGEN_WORDS.get("nogluten", []))


def _hay(word: str, tl: str) -> str:
    """Текст, по которому ищем запрещённое слово: для молочных и глютеновых корней —
    очищенный от «ложных друзей» (растительное молоко, томатная паста)."""
    if word in _LACT_WORDS:
        return _LACT_OK.sub(" ", tl)
    if word in _GLU_WORDS:
        return _GLU_OK.sub(" ", tl)
    return tl


# Слова, которые означают не мясо, а форму подачи: «Картофельные котлеты», «Стейк из
# лосося», «фарш из чечевицы». Для блюда ИЗ КАТАЛОГА мясо определяет точный флаг meat,
# и рубить его по этим словам нельзя — иначе валидатор заворачивает ровно те блюда,
# которые сам каталог и предложил, и вегетарианец уезжает в банк-план без рецептов.
_MEAT_FORM_WORDS = ("котлет", "стейк", "фарш")


def _violates(text: str, quiz: dict, excl: list[str] | None = None,
              ignore_words: tuple[str, ...] = ()) -> bool:
    """Нарушает ли текст (название+ингредиенты блюда) ограничения/аллергии/исключения."""
    tl = (text or "").lower()
    for w in _forbidden_words(quiz):
        if w in ignore_words:
            continue
        if w in _hay(w, tl):
            return True
    for t in (excl if excl is not None else _excluded_terms(quiz)):
        if _term_hits(t, text):
            return True
    return False


def _allowed_by_meal(quiz: dict) -> dict:
    """Каталог, отфильтрованный под ограничения + аллергии + исключения, по приёму пищи."""
    diet = set(quiz.get("diet") or [])
    nomeat, nofish, nolact = "nomeat" in diet, "nofish" in diet, "nolact" in diet
    excl = _excluded_terms(quiz)
    # В каталоге мясо определяет флаг, поэтому словарь по nomeat здесь не нужен —
    # он только отнимал бы вегетарианские блюда со «спорными» названиями.
    fwords = _forbidden_words(quiz, skip=("nomeat",))
    disliked = {str(t).strip().lower() for t in (quiz.get("disliked") or [])}
    groups: dict[str, list] = {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
    for d in _catalog():
        # «Без мяса» режет ИМЕННО мясо (флаг meat), а не «всё, что не помечено veg».
        # Тег veg был непоследователен: «Омлет с овощами» veg=true, а «Вареные яйца»
        # veg=false — и человек без мяса терял одиннадцать яичных блюд, включая
        # почти все белковые завтраки. Рыба режется отдельным флагом nofish.
        if nomeat and d.get("meat"):
            continue
        if nofish and d.get("fish"):
            continue
        if nolact and d.get("lact"):
            continue
        tl = d["title"].lower()
        # Молочные/глютеновые корни ищем по тексту, очищенному от «ложных друзей»
        # (растительное молоко, томатная паста) — см. _hay.
        if any(w in _hay(w, tl) for w in fwords):
            continue                            # аллерген/запрещённый продукт
        if any(_term_hits(t, tl) for t in excl):  # исключённый продукт (стем-матч)
            continue
        if tl in disliked:
            continue
        groups.setdefault(d.get("meal", "lunch"), []).append(d["title"])
    return groups


def _plan_violations(pl: dict, quiz: dict) -> list[str]:
    """Названия/ингредиенты блюд плана, нарушающие ограничения — пост-валидация LLM-выхода."""
    excl = _excluded_terms(quiz)
    bad = []
    for d in pl.get("days") or []:
        for m in d.get("meals") or []:
            text = (m.get("name", "") + " " + " ".join(str(x) for x in (m.get("ingredients") or [])))
            if _violates(text, quiz, excl):
                bad.append(m.get("name", ""))
    return bad


def _norm_names(names) -> set[str]:
    return {str(n).strip().lower() for n in (names or []) if str(n).strip()}


def _catalog_block(quiz: dict, avoid: list[str] | None = None) -> str:
    groups = _allowed_by_meal(quiz)
    used = _norm_names(avoid)
    lines = []
    for meal, label in [("breakfast", "Завтраки"), ("lunch", "Обеды"),
                        ("dinner", "Ужины"), ("snack", "Перекусы")]:
        names = groups.get(meal) or []
        if used:
            fresh = [n for n in names if n.strip().lower() not in used]
            # Прошлую неделю убираем из каталога только если на новую всё равно хватает
            # (7 = неделя): у жёстких ограничений список короткий, и вычитание оставило бы
            # модель без выбора — тогда лучше повтор, чем пустой раздел.
            if len(fresh) >= 7:
                names = fresh
        if names:
            lines.append(f"{label}: " + "; ".join(names))
    return "\n".join(lines)


def _prompt(quiz: dict, p: dict, avoid: list[str] | None = None) -> tuple[str, str]:
    meals = MEALS_RU.get(quiz.get("meals"), "3 приёма пищи")
    system = ("Ты опытный нутрициолог. Составляешь персональные, реалистичные и вкусные планы питания "
              "из продуктов, доступных в обычном российском супермаркете. Отвечаешь ТОЛЬКО валидным JSON "
              "по заданной схеме — без markdown, без комментариев.")
    catalog = _catalog_block(quiz, avoid)
    # Список прошлой недели режем: 40 названий модель ещё удерживает, а весь архив
    # подписки раздул бы промпт и утопил в нём остальные требования.
    prev = list(dict.fromkeys(str(n).strip() for n in (avoid or []) if str(n).strip()))[:40]
    catalog_rule = (
        "- Поле name КАЖДОГО блюда выбирай ТОЛЬКО из каталога ниже и пиши название ТОЧНО как в списке "
        "(подбирай под приём пищи, цель и норму; разнообразь по дням; любимое — чаще). Рецепт, ингредиенты, "
        "порции и КБЖУ придумывай сам под норму.\n"
    ) if catalog else ""
    catalog_txt = f"\nКАТАЛОГ БЛЮД (выбирай name только отсюда):\n{catalog}\n" if catalog else ""
    user = (
        f"Составь план питания на 7 дней (День 1 – День 7; план стартует в день покупки,\n"
        f"дни недели не используй).\n"
        f"Дневная норма: {p['cal']} ккал (белки {p['P']} г, жиры {p['F']} г, углеводы {p['C']} г).\n"
        f"Цель: {GOAL_RU.get(quiz.get('goal'), 'здоровое питание')}.\n"
        f"Активность: {ACT_RU.get(quiz.get('activity'), '—')}.\n"
        f"Формат питания: {meals}.\n"
        f"Время на готовку: {COOK_RU.get(quiz.get('cook'), '20–30 минут')}.\n"
        f"Ограничения (соблюдать СТРОГО): {_labels(quiz.get('diet'), DIET_RU)}.\n"
        f"Любит: {_labels(quiz.get('favorites'), FAV_RU)}.\n"
        f"Что мешало раньше: {_labels(quiz.get('barriers'), BARR_RU)}.\n\n"
        "Требования:\n"
        "- Каждый день укладывается в норму калорий ±7%, БЖУ примерно по цели.\n"
        + (f"- СТРОГО НЕ используй эти продукты и любые блюда/рецепты с ними: {quiz.get('exclude')}.\n"
           if (quiz.get('exclude') or '').strip() else "")
        + (f"- НЕ предлагай эти блюда (пользователю не понравились): {', '.join(quiz.get('disliked') or [])}.\n"
           if (quiz.get('disliked') or []) else "")
        + (f"- Это следующая неделя: НЕ повторяй блюда прошлой недели — {'; '.join(prev)}. "
           "Если из-за ограничений выбора почти не осталось, повтори минимум блюд и "
           "поставь их в другие дни/приёмы.\n" if prev else "")
        + f"{catalog_rule}"
        "- Блюда разнообразные по дням, простые рецепты (3–6 шагов), ингредиенты — из РФ-магазина.\n"
        "- СТРОГО уважать ограничения (веган/без лактозы/без глютена/без рыбы/орехи).\n"
        "- Добавлять любимые продукты чаще; учитывать время на готовку и число приёмов.\n"
        "- Порции указывать в граммах/штуках в ингредиентах.\n"
        f"{catalog_txt}\n"
        "Верни СТРОГО такой JSON:\n"
        '{"days":[{"day":"День 1","meals":[{"slot":"Завтрак","name":"...","kcal":000,'
        '"p":00,"f":00,"c":00,"ingredients":["овсянка 60 г","молоко 200 мл"],"steps":["шаг 1","шаг 2"]}]}],'
        '"shopping":[{"cat":"Овощи и зелень","items":["брокколи 500 г"]}],'
        '"tips":["короткий практичный совет под цель и барьеры"]}'
    )
    return system, user


def _plan_problems(days, cal: int, quiz: dict | None = None,
                   shopping=None) -> tuple[list[str], list[str]]:
    """Претензии к LLM-плану: (критичные, некритичные).

    Критичные — отдать такой план нельзя: не 7 дней, день без приёмов/названий,
    аллерген или запрещённый продукт. Некритичные — план кривой, но съедобный:
    ккал мимо коридора, блюдо без рецепта, пустой список покупок, одно и то же
    блюдо в одном приёме два дня подряд. Разделение нужно, чтобы валидатор был
    строгим (ретрай), но не сваливал выдачу в банк-план БЕЗ рецептов из-за
    придирки — банк заведомо хуже кривого AI-плана.
    """
    if not isinstance(days, list) or len(days) != 7:
        return ["days"], []
    hard: list[str] = []
    soft: list[str] = []
    excl = _excluded_terms(quiz or {})
    # Блюда, которые каталог сам предложил под эти ограничения (там мясо/рыба/лактоза
    # определены точным флагом): по ним словарные «котлет/стейк/фарш» не считаем.
    from_catalog = {t.lower() for names in _allowed_by_meal(quiz or {}).values() for t in names}
    prev_by_slot: dict[str, str] = {}
    for idx, d in enumerate(days):
        meals = (d or {}).get("meals") or []
        if len(meals) < 2 or any(not (m.get("name") or "").strip() for m in meals):
            hard.append(f"meals:{idx}")
            continue
        try:
            s = sum(int(m.get("kcal") or 0) for m in meals)
        except Exception:  # noqa: BLE001
            s = 0
        if cal and abs(s - cal) > cal * 0.15:
            soft.append(f"kcal:{idx}")
        cur_by_slot: dict[str, str] = {}
        for m in meals:
            name = (m.get("name") or "").strip()
            text = name + " " + " ".join(str(x) for x in (m.get("ingredients") or []))
            ignore = _MEAT_FORM_WORDS if name.lower() in from_catalog else ()
            if quiz and _violates(text, quiz, excl, ignore):
                hard.append(f"diet:{name}")
            if not (m.get("ingredients") or []) or not (m.get("steps") or []):
                soft.append(f"recipe:{name}")  # продаём рецепты — блюдо без них товар не выполняет
            slot = (m.get("slot") or "").strip().lower()
            cur_by_slot[slot] = name.lower()
            if prev_by_slot.get(slot) == name.lower():
                soft.append(f"repeat:{name}")
        prev_by_slot = cur_by_slot
    if not (shopping or []):
        soft.append("shopping")  # список покупок пуст → в плане есть еда, которой не на что купить
    return hard, soft


def _plan_ok(days, cal: int, quiz: dict | None = None, shopping=None) -> bool:
    """План проходит валидацию полностью (ни критичных, ни некритичных претензий)."""
    hard, soft = _plan_problems(days, cal, quiz, shopping)
    return not hard and not soft


def _fix_note(hard: list[str], soft: list[str]) -> str:
    """Приписка к промпту на ретрай: что именно было не так в прошлой попытке."""
    bad = [i.split(":", 1)[1] for i in hard + soft if i.startswith(("diet:", "repeat:")) and ":" in i]
    parts = []
    if bad:
        parts.append("не используй блюда " + ", ".join(dict.fromkeys(bad))
                     + " (нарушают ограничения или повторяются два дня подряд)")
    if any(i.startswith("recipe:") for i in soft):
        parts.append("у КАЖДОГО блюда заполни ingredients и steps")
    if "shopping" in soft:
        parts.append("заполни shopping — список покупок по всем блюдам недели")
    if any(i.startswith("kcal:") for i in soft):
        parts.append("сумма ккал каждого дня должна укладываться в норму ±7%")
    if any(i in ("days", "meals") or i.startswith("meals:") for i in hard):
        parts.append("ровно 7 дней, в каждом дне все приёмы пищи с названиями")
    return ("\n\nПрошлая попытка забракована: " + "; ".join(parts) + ".") if parts else ""


def generate_plan(quiz: dict, timeout: int = 90, attempts: int = 2,
                  avoid: list[str] | None = None) -> dict:
    """avoid — названия блюд прошлых недель: их не повторяем (иначе неделя 2 при том же
    квизе выходит копией недели 1). Необязателен: без него поведение прежнее."""
    p = _plan.compute(quiz)
    key = _key()
    base = {"cal": p["cal"], "P": p["P"], "F": p["F"], "C": p["C"], "goal": p["goal"]}
    if not key:
        return {**base, "source": "bank", **_bank_shape(quiz, avoid)}
    system, user = _prompt(quiz, p, avoid)
    soft_best: dict | None = None
    fix_note = ""
    for attempt in range(attempts):
        body = {
            "model": TEXT_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user + fix_note}],
            "response_format": {"type": "json_object"},
            # первый заход — обычная температура; ретрай — консервативнее (меньше брака)
            "temperature": 0.6 if attempt == 0 else 0.3,
        }
        req = urllib.request.Request(
            OPENROUTER_URL, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://mynutriplan.ru", "X-Title": "NutriPlan"})
        try:
            with _urlopen(req, timeout) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception:  # noqa: BLE001 — сеть/невалидный JSON: ретрай, после всех попыток → банк
            continue
        days = parsed.get("days") or []
        shopping = parsed.get("shopping") or []
        hard, soft = _plan_problems(days, p["cal"], quiz, shopping)
        # Ретрай с тем же промптом обычно ломается на том же месте, поэтому претензию
        # передаём модели: одно плохое блюдо из 35 иначе стоит всего плана (уход в банк).
        fix_note = _fix_note(hard, soft)
        if hard:
            continue  # аллерген/нет 7 дней — отдавать нельзя ни при каких условиях
        result = {**base, "source": "ai", "days": days,
                  "shopping": shopping, "tips": parsed.get("tips") or []}
        if not soft:
            return result
        if soft_best is None:
            soft_best = result  # придержим и попробуем ещё раз — вдруг выйдет чистый
    if soft_best:
        return soft_best
    # банк-fallback (без рецептов/покупок) — деградация; cron дорегенерирует (source=bank)
    return {**base, "source": "bank", **_bank_shape(quiz, avoid)}


# План стартует в день покупки, а не в понедельник, поэтому дни нумеруем.
# Отображение всё равно считается от индекса (plan._day_label), но пусть и в
# данных не остаётся дней недели, вводящих в заблуждение в письмах и выгрузках.
_DAYS_RU = [f"День {i}" for i in range(1, 8)]


def _reorder_pool(pool: list, used: set[str], shift: int) -> list:
    """Пул под новую неделю: провернуть на shift и увести блюда прошлой недели в конец.

    Без этого pool[i % len(pool)] при том же квизе даёт БУКВАЛЬНО ту же неделю.
    Порядок остаётся детерминированным (тот же avoid → тот же план), но другим.
    Прошлую неделю не выбрасываем, а опускаем вниз: при жёстких ограничениях пул
    короткий, и выбросив его целиком мы остались бы без блюд."""
    if not pool:
        return pool
    off = shift % len(pool)
    pool = pool[off:] + pool[:off]
    fresh = [n for n in pool if n.strip().lower() not in used]
    return fresh + [n for n in pool if n.strip().lower() in used]


def _bank_shape(quiz: dict, avoid: list[str] | None = None) -> dict:
    """Fallback без LLM: собрать неделю из ОТФИЛЬТРОВАННОГО каталога (уже без рыбы/лактозы/
    аллергенов/исключений) — а не из статичного BANK, который игнорировал всё кроме «без мяса».
    Без рецептов/списка (их даёт только LLM), но безопасно по ограничениям."""
    p = _plan.compute(quiz)
    cal = p["cal"]
    groups = _allowed_by_meal(quiz)
    used = _norm_names(avoid)
    # Сдвиг считаем от прошлой недели, а не от random: план должен быть воспроизводим.
    # hash() не годится — он рандомизирован между процессами (PYTHONHASHSEED).
    shift = int(hashlib.blake2s("|".join(sorted(used)).encode()).hexdigest()[:8], 16) if used else 0
    # приёмы: завтрак/обед/ужин (+перекус если каталог есть) с долями ккал
    slots = [("Завтрак", "breakfast", 0.30), ("Обед", "lunch", 0.40), ("Ужин", "dinner", 0.30)]
    pools = {k: _reorder_pool(groups.get(k) or groups.get("lunch") or ["Сбалансированное блюдо"],
                              used, shift) for _, k, _ in slots}
    days = []
    for i in range(7):
        meals = []
        for label, key, share in slots:
            pool = pools[key]
            name = pool[i % len(pool)] if pool else "Сбалансированное блюдо"
            meals.append({"slot": label, "name": name, "kcal": int(round(cal * share / 10) * 10),
                          "ingredients": [], "steps": []})
        days.append({"day": _DAYS_RU[i], "meals": meals})
    return {"days": days, "shopping": [], "tips": []}


def swap_meal(quiz: dict, slot: str, kcal: int, avoid: str = "") -> dict | None:
    """Перегенерить ОДНО блюдо для приёма (замена), с учётом ограничений и калорий."""
    key = _key()
    if not key:
        return None
    # каталог под слот (Завтрак/Обед/Ужин/Перекус) → у замены тоже гарантированно есть фото
    _slot_meal = {"завтрак": "breakfast", "обед": "lunch", "ужин": "dinner", "перекус": "snack"}
    meal_key = _slot_meal.get((slot or "").strip().lower(), "lunch")
    names = _allowed_by_meal(quiz).get(meal_key) or []
    if avoid:
        names = [n for n in names if n.strip().lower() != avoid.strip().lower()]
    from_catalog = {n.strip().lower() for n in names}
    catalog_line = (f"Выбери name ТОЛЬКО из этого списка (точно как написано): {'; '.join(names)}.\n"
                    if names else "")
    system = "Ты нутрициолог. Отвечаешь ТОЛЬКО валидным JSON, без markdown."
    user = (
        f"Предложи ОДНО альтернативное блюдо для приёма «{slot}», примерно {kcal} ккал.\n"
        f"Ограничения (соблюдать СТРОГО): {_labels(quiz.get('diet'), DIET_RU)}.\n"
        f"Любит: {_labels(quiz.get('favorites'), FAV_RU)}. Время на готовку: {COOK_RU.get(quiz.get('cook'), '20–30 минут')}.\n"
        f"Продукты — из российского магазина. НЕ предлагай: {avoid or '—'}.\n"
        + (f"СТРОГО НЕ используй эти продукты: {quiz.get('exclude')}.\n"
           if (quiz.get('exclude') or '').strip() else "")
        + f"{catalog_line}"
        'Верни JSON: {"slot":"' + slot + '","name":"...","kcal":000,"p":00,"f":00,"c":00,'
        '"ingredients":["продукт 100 г"],"steps":["шаг 1","шаг 2"]}'
    )
    body = {"model": TEXT_MODEL, "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}], "response_format": {"type": "json_object"}, "temperature": 0.85}
    # Провайдер регулярно отвечает 200 с finish_reason=error и обрубком вроде '{"slot":"'
    # (usage нулевой — генерации не было). Одной попытки не хватало: тап «не нравится»
    # получал 502 и всё равно списывал одну из 20 замен в час. Ретраим только разбор.
    m = None
    for _ in range(2):
        req = urllib.request.Request(
            OPENROUTER_URL, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://mynutriplan.ru", "X-Title": "NutriPlan"})
        try:
            with _urlopen(req, 60) as r:
                data = json.loads(r.read())
            m = json.loads(data["choices"][0]["message"]["content"])
            break
        except Exception:  # noqa: BLE001
            m = None
    try:
        if not m or not m.get("name"):
            return None
        # Валидация замены: калории ±30%, не нарушает диету/аллергии/исключения, не avoid.
        try:
            if abs(int(m.get("kcal") or 0) - int(kcal)) > int(kcal) * 0.3:
                return None
        except Exception:  # noqa: BLE001
            return None
        text = m.get("name", "") + " " + " ".join(str(x) for x in (m.get("ingredients") or []))
        # То же послабление, что и в валидаторе плана: у блюда ИЗ нашего же списка мясо
        # определяет флаг meat, поэтому «котлет/стейк/фарш» в названии — это форма подачи.
        # Иначе замена предлагала вегетарианцу «Чечевичные котлеты» из своего каталога и
        # сама же их заворачивала — тап «не нравится» отвечал 502 и жёг лимит замен.
        ignore = _MEAT_FORM_WORDS if m["name"].strip().lower() in from_catalog else ()
        if _violates(text, quiz, None, ignore):
            return None
        if avoid and m["name"].strip().lower() == avoid.strip().lower():
            return None
        m["slot"] = slot
        return m
    except Exception:  # noqa: BLE001
        return None


def swap_day(quiz: dict, meals: list, timeout: int = 90) -> list | None:
    """Перегенерить ВЕСЬ день целиком, сохранив состав приёмов и калорийность.

    Одним запросом, а не пятью подряд по swap_meal: пять последовательных
    вызовов — это минута ожидания и пять шансов получить 502, а главное, блюда
    подбирались бы независимо и день переставал быть днём (три помидорных блюда
    подряд — обычный исход).

    Возвращает список приёмов той же длины и с теми же слотами либо None: день
    заменяется целиком или не заменяется вовсе, половина нового дня хуже
    старого.
    """
    key = _key()
    if not key or not meals:
        return None
    slots = [(m.get("slot") or "").strip() for m in meals]
    kcals = []
    for m in meals:
        try:
            kcals.append(int(float(m.get("kcal") or 0)))
        except Exception:  # noqa: BLE001
            kcals.append(400)
    total = sum(kcals)
    avoid = [m.get("name", "") for m in meals if m.get("name")]
    plan_line = "; ".join(f"{s} ~{k} ккал" for s, k in zip(slots, kcals))
    system = "Ты нутрициолог. Отвечаешь ТОЛЬКО валидным JSON, без markdown."
    user = (
        f"Собери НОВЫЙ вариант дня на {total} ккал: {plan_line}.\n"
        f"Ограничения (соблюдать СТРОГО): {_labels(quiz.get('diet'), DIET_RU)}.\n"
        f"Любит: {_labels(quiz.get('favorites'), FAV_RU)}. "
        f"Время на готовку: {COOK_RU.get(quiz.get('cook'), '20–30 минут')}.\n"
        f"Продукты — из российского магазина. НЕ повторяй эти блюда: {', '.join(avoid) or '—'}.\n"
        + (f"СТРОГО НЕ используй эти продукты: {quiz.get('exclude')}.\n"
           if (quiz.get("exclude") or "").strip() else "")
        + _catalog_block(quiz, avoid)
        + 'Верни JSON: {"meals":[{"slot":"...","name":"...","kcal":000,"p":00,"f":00,"c":00,'
          '"ingredients":["продукт 100 г"],"steps":["шаг 1","шаг 2"]}]} — '
          f"ровно {len(meals)} приёмов, слоты и порядок как в запросе."
    )
    body = {"model": TEXT_MODEL, "messages": [{"role": "system", "content": system},
            {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}, "temperature": 0.85}
    got = None
    for _ in range(2):     # тот же обрубок '{"meals":[' от провайдера, что и в swap_meal
        req = urllib.request.Request(
            OPENROUTER_URL, data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "HTTP-Referer": "https://mynutriplan.ru", "X-Title": "NutriPlan"})
        try:
            with _urlopen(req, timeout) as r:
                data = json.loads(r.read())
            got = json.loads(data["choices"][0]["message"]["content"]).get("meals")
            if got:
                break
        except Exception:  # noqa: BLE001
            got = None
    if not isinstance(got, list) or len(got) != len(meals):
        return None
    old_names = {n.strip().lower() for n in avoid}
    allowed = _allowed_by_meal(quiz)
    from_catalog = {n.strip().lower() for v in allowed.values() for n in v}
    out = []
    for i, m in enumerate(got):
        if not isinstance(m, dict) or not m.get("name"):
            return None
        try:
            if abs(int(m.get("kcal") or 0) - kcals[i]) > max(80, kcals[i] * 0.35):
                return None
        except Exception:  # noqa: BLE001
            return None
        text = m.get("name", "") + " " + " ".join(str(x) for x in (m.get("ingredients") or []))
        # То же послабление, что и в swap_meal: у блюда из нашего каталога «котлета» —
        # это форма подачи, а не мясо (флаг meat уже проверен при отборе каталога).
        ignore = _MEAT_FORM_WORDS if m["name"].strip().lower() in from_catalog else ()
        if _violates(text, quiz, None, ignore):
            return None
        if m["name"].strip().lower() in old_names:
            return None          # «замена» тем же блюдом — не замена
        m["slot"] = slots[i]
        out.append(m)
    return out
