"""LLM-генерация персонального плана питания (структурированный JSON).

Учитывает ВСЕ сигналы квиза: цель, норму КБЖУ, ограничения, любимое, число
приёмов, время на готовку, барьеры. Продукты — доступные в РФ. При сбое —
fallback на статичный банк (plan.week).

Выход (dict): {"cal","P","F","C","goal","days":[{day,meals:[{slot,name,kcal,p,f,c,ingredients[],steps[]}]}],
               "shopping":[{cat,items[]}], "tips":[...]}
"""
from __future__ import annotations

import json
import os
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
                 "манты", "хинкал", "чебурек", "блинчик", "тортилья", "кекс", "маффин", "сэндвич"],
    "nomeat": ["курин", "куриц", "говяд", "свин", "телятин", "баранин", "индейк", "мясн",
               "фарш", "котлет", "бекон", "колбас", "сосиск", "ветчин", "буженин", "стейк"],
    "nofish": ["рыб", "лосос", "тунец", "форел", "сельд", "скумбри", "треск", "минтай",
               "креветк", "кальмар", "мидии", "морепродукт", "икр", "краб", "суши", "роллы"],
    "nolact": ["молок", "творог", "сыр", "йогурт", "кефир", "сметан", "сливк", "масл сливоч",
               "ряженк", "брынз", "моцарелл", "маскарпоне", "сырник"],
}


def _forbidden_words(quiz: dict) -> list[str]:
    """Слова-маркеры, запрещённые диетой/аллергиями пользователя."""
    diet = set(quiz.get("diet") or [])
    words: list[str] = []
    for code in diet:
        words += _ALLERGEN_WORDS.get(code, [])
    return words


def _violates(text: str, quiz: dict, excl: list[str] | None = None) -> bool:
    """Нарушает ли текст (название+ингредиенты блюда) ограничения/аллергии/исключения."""
    tl = (text or "").lower()
    for w in _forbidden_words(quiz):
        if w in tl:
            return True
    for t in (excl if excl is not None else _excluded_terms(quiz)):
        if _term_hits(t, text):
            return True
    return False


def _allowed_by_meal(quiz: dict) -> dict:
    """Каталог, отфильтрованный под ограничения + аллергии + исключения, по приёму пищи."""
    diet = set(quiz.get("diet") or [])
    veg, nofish, nolact = "nomeat" in diet, "nofish" in diet, "nolact" in diet
    excl = _excluded_terms(quiz)
    fwords = _forbidden_words(quiz)
    disliked = {str(t).strip().lower() for t in (quiz.get("disliked") or [])}
    groups: dict[str, list] = {"breakfast": [], "lunch": [], "dinner": [], "snack": []}
    for d in _catalog():
        if veg and not d.get("veg"):
            continue
        if nofish and d.get("fish"):
            continue
        if nolact and d.get("lact"):
            continue
        tl = d["title"].lower()
        if any(w in tl for w in fwords):        # аллерген/запрещённый продукт (орехи, глютен, мясо…)
            continue
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


def _catalog_block(quiz: dict) -> str:
    groups = _allowed_by_meal(quiz)
    lines = []
    for meal, label in [("breakfast", "Завтраки"), ("lunch", "Обеды"),
                        ("dinner", "Ужины"), ("snack", "Перекусы")]:
        names = groups.get(meal) or []
        if names:
            lines.append(f"{label}: " + "; ".join(names))
    return "\n".join(lines)


def _prompt(quiz: dict, p: dict) -> tuple[str, str]:
    meals = MEALS_RU.get(quiz.get("meals"), "3 приёма пищи")
    system = ("Ты опытный нутрициолог. Составляешь персональные, реалистичные и вкусные планы питания "
              "из продуктов, доступных в обычном российском супермаркете. Отвечаешь ТОЛЬКО валидным JSON "
              "по заданной схеме — без markdown, без комментариев.")
    catalog = _catalog_block(quiz)
    catalog_rule = (
        "- Поле name КАЖДОГО блюда выбирай ТОЛЬКО из каталога ниже и пиши название ТОЧНО как в списке "
        "(подбирай под приём пищи, цель и норму; разнообразь по дням; любимое — чаще). Рецепт, ингредиенты, "
        "порции и КБЖУ придумывай сам под норму.\n"
    ) if catalog else ""
    catalog_txt = f"\nКАТАЛОГ БЛЮД (выбирай name только отсюда):\n{catalog}\n" if catalog else ""
    user = (
        f"Составь план питания на 7 дней (Понедельник–Воскресенье).\n"
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
        + f"{catalog_rule}"
        "- Блюда разнообразные по дням, простые рецепты (3–6 шагов), ингредиенты — из РФ-магазина.\n"
        "- СТРОГО уважать ограничения (веган/без лактозы/без глютена/без рыбы/орехи).\n"
        "- Добавлять любимые продукты чаще; учитывать время на готовку и число приёмов.\n"
        "- Порции указывать в граммах/штуках в ингредиентах.\n"
        f"{catalog_txt}\n"
        "Верни СТРОГО такой JSON:\n"
        '{"days":[{"day":"Понедельник","meals":[{"slot":"Завтрак","name":"...","kcal":000,'
        '"p":00,"f":00,"c":00,"ingredients":["овсянка 60 г","молоко 200 мл"],"steps":["шаг 1","шаг 2"]}]}],'
        '"shopping":[{"cat":"Овощи и зелень","items":["брокколи 500 г"]}],'
        '"tips":["короткий практичный совет под цель и барьеры"]}'
    )
    return system, user


def _plan_ok(days, cal: int, quiz: dict | None = None) -> bool:
    """Валидация LLM-плана: 6–7 дней, ≥2 приёма/день с названиями, дневная сумма ккал в
    ±15% от цели, и НИ ОДНОГО нарушения диеты/аллергий/исключений (безопасность > полнота)."""
    if not (isinstance(days, list) and 6 <= len(days) <= 7):
        return False
    excl = _excluded_terms(quiz or {})
    for d in days:
        meals = d.get("meals") or []
        if len(meals) < 2 or any(not (m.get("name") or "").strip() for m in meals):
            return False
        try:
            s = sum(int(m.get("kcal") or 0) for m in meals)
        except Exception:  # noqa: BLE001
            return False
        if cal and abs(s - cal) > cal * 0.15:
            return False
        if quiz:
            for m in meals:
                text = m.get("name", "") + " " + " ".join(str(x) for x in (m.get("ingredients") or []))
                if _violates(text, quiz, excl):
                    return False  # аллерген/запрещённый продукт в плане — брак, ретраим
    return True


def generate_plan(quiz: dict, timeout: int = 90, attempts: int = 2) -> dict:
    p = _plan.compute(quiz)
    key = _key()
    base = {"cal": p["cal"], "P": p["P"], "F": p["F"], "C": p["C"], "goal": p["goal"]}
    if not key:
        return {**base, "source": "bank", **_bank_shape(quiz)}
    system, user = _prompt(quiz, p)
    for attempt in range(attempts):
        body = {
            "model": TEXT_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
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
            days = parsed.get("days") or []
            if not _plan_ok(days, p["cal"], quiz):
                raise ValueError("plan failed validation")
            return {**base, "source": "ai", "days": days,
                    "shopping": parsed.get("shopping") or [], "tips": parsed.get("tips") or []}
        except Exception:  # noqa: BLE001 — ретрай; после всех попыток → банк
            continue
    # банк-fallback (без рецептов/покупок) — деградация; cron дорегенерирует (source=bank)
    return {**base, "source": "bank", **_bank_shape(quiz)}


_DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _bank_shape(quiz: dict) -> dict:
    """Fallback без LLM: собрать неделю из ОТФИЛЬТРОВАННОГО каталога (уже без рыбы/лактозы/
    аллергенов/исключений) — а не из статичного BANK, который игнорировал всё кроме «без мяса».
    Без рецептов/списка (их даёт только LLM), но безопасно по ограничениям."""
    p = _plan.compute(quiz)
    cal = p["cal"]
    groups = _allowed_by_meal(quiz)
    # приёмы: завтрак/обед/ужин (+перекус если каталог есть) с долями ккал
    slots = [("Завтрак", "breakfast", 0.30), ("Обед", "lunch", 0.40), ("Ужин", "dinner", 0.30)]
    pools = {k: (groups.get(k) or groups.get("lunch") or ["Сбалансированное блюдо"]) for _, k, _ in slots}
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
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://mynutriplan.ru", "X-Title": "NutriPlan"})
    try:
        with _urlopen(req, 60) as r:
            data = json.loads(r.read())
        m = json.loads(data["choices"][0]["message"]["content"])
        if not m.get("name"):
            return None
        # Валидация замены: калории ±30%, не нарушает диету/аллергии/исключения, не avoid.
        try:
            if abs(int(m.get("kcal") or 0) - int(kcal)) > int(kcal) * 0.3:
                return None
        except Exception:  # noqa: BLE001
            return None
        text = m.get("name", "") + " " + " ".join(str(x) for x in (m.get("ingredients") or []))
        if _violates(text, quiz):
            return None
        if avoid and m["name"].strip().lower() == avoid.strip().lower():
            return None
        m["slot"] = slot
        return m
    except Exception:  # noqa: BLE001
        return None
