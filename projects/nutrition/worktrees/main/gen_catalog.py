#!/usr/bin/env python3
"""Генерация каталога блюд (одноразово) → dishes.json.

LLM выдаёт ~N канонических рус-блюд с мета: приём пищи, вег-флаг. Каталог ограничивает
словарь генерации плана → конечный набор фото. Запускать в контейнере (ключ + LLM_PROXY).
"""
import json
import os
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.getenv("NB_TEXT_MODEL", "google/gemini-2.5-flash")


def _urlopen(req, timeout):
    proxy = os.getenv("LLM_PROXY", "").strip()
    if proxy:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return op.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


SYSTEM = ("Ты шеф-повар и нутрициолог российского приложения питания. Отвечаешь ТОЛЬКО валидным JSON.")

USER = """Составь каталог из 170 популярных, реалистичных, вкусных блюд для россиян.
Распредели примерно: 45 завтраков, 50 обедов, 50 ужинов, 25 перекусов.
Блюда — из обычного российского супермаркета, разнообразные (мясо, птица, рыба, крупы, овощи,
творог/яйца, вегетарианские). Каноничные КОРОТКИЕ названия без граммовок и без слова «домашний».
Не дублируй по сути (не «Овсянка с ягодами» и «Овсяная каша с ягодами» одновременно).

Для каждого блюда:
- "title": рус. название (2-5 слов, с заглавной)
- "meal": одно из breakfast|lunch|dinner|snack
- "veg": true если БЕЗ мяса, птицы, рыбы и морепродуктов (иначе false)
- "fish": true если содержит рыбу/морепродукты (иначе false)
- "lact": true если содержит молочку/творог/сыр (иначе false)

Верни СТРОГО JSON: {"dishes":[{"title":"...","meal":"breakfast","veg":false,"fish":false,"lact":false}, ...]}"""


def main():
    key = os.environ["OPENROUTER_API_KEY"]
    body = {"model": MODEL, "response_format": {"type": "json_object"}, "temperature": 0.7,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]}
    req = urllib.request.Request(OPENROUTER_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "HTTP-Referer": "https://mynutriplan.ru", "X-Title": "NutriPlan catalog"})
    with _urlopen(req, 180) as r:
        data = json.loads(r.read())
    dishes = json.loads(data["choices"][0]["message"]["content"]).get("dishes") or []
    # дедуп по нормализованному slug
    import dish_photos as dp
    seen, out = set(), []
    for d in dishes:
        t = (d.get("title") or "").strip()
        if not t:
            continue
        s = dp.slugify(t)
        if s in seen:
            continue
        seen.add(s)
        out.append({"slug": s, "title": t, "meal": d.get("meal", "lunch"),
                    "veg": bool(d.get("veg")), "fish": bool(d.get("fish")), "lact": bool(d.get("lact"))})
    json.dump({"dishes": out}, open("dishes.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    by_meal = {}
    for d in out:
        by_meal[d["meal"]] = by_meal.get(d["meal"], 0) + 1
    print(f"каталог: {len(out)} блюд, по приёмам: {by_meal}, вег: {sum(1 for d in out if d['veg'])}")


main()
