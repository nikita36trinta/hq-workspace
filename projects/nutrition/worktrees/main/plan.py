"""Серверный расчёт персонального плана + генерация меню и HTML-писем.

Зеркалит расчёт из quiz.html (Mifflin-St Jeor). Меню — из курируемого банка блюд,
порции считаются от нормы калорий пользователя (30/40/30). Учитывает «без мяса».
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import quote

import dish_photos


def _e(s) -> str:
    """HTML-экранирование ЛЮБОГО динамического текста (LLM-вывод, поля юзера) — защита от XSS."""
    return html.escape(str(s if s is not None else ""), quote=True)

GOAL_TXT = {"lose": "снижения веса", "keep": "поддержания формы",
            "gain": "набора массы", "health": "здорового питания"}
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _ic_home() -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M4 11 12 4l8 7v8a2 2 0 0 1-2 2h-3v-6H9v6H6a2 2 0 0 1-2-2Z"/></svg>')


def _ic_week() -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round"><rect x="3" y="5" width="18" height="16" rx="3"/>'
            '<path d="M8 3v4M16 3v4M3 10h18"/></svg>')


def _ic_cart() -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M3 4h2l2.2 10.2a2 2 0 0 0 2 1.6h7.4'
            'a2 2 0 0 0 2-1.5L20 8H6"/><circle cx="10" cy="20" r="1.3"/><circle cx="17" cy="20" r="1.3"/></svg>')


def _ic_me() -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round"><circle cx="12" cy="8" r="3.6"/>'
            '<path d="M4.5 20c1.3-3.6 4-5.4 7.5-5.4S18.2 16.4 19.5 20"/></svg>')


def _ic_back() -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
            'stroke-linecap="round" stroke-linejoin="round"><path d="M15 5l-7 7 7 7"/></svg>')


def _day_label(i: int) -> str:
    """«День 1…7», а не «Понедельник».

    План начинается в день покупки, а не в понедельник: купивший в четверг видел
    «Понедельник» первым днём и не понимал, к какой дате это относится. Считаем
    номер от индекса, а не от того, что вернула модель, — тогда подпись не
    зависит от её фантазии и одинакова у ai- и банк-плана."""
    return f"День {i + 1}"

BANK = {
    "omni": [
        ("Овсянка с ягодами и орехами", "Курица с киноа и овощами", "Запечённая рыба и салат"),
        ("Омлет с овощами и цельнозерновой тост", "Говядина с гречкой и брокколи", "Творожная запеканка и салат"),
        ("Греческий йогурт с гранолой и бананом", "Индейка с булгуром и овощами", "Треска с тушёными овощами"),
        ("Сырники с ягодным соусом", "Куриный суп и цельнозерновой хлеб", "Тёплый салат с говядиной"),
        ("Яйца пашот на авокадо-тосте", "Лосось с рисом и спаржей", "Овощное рагу с курицей"),
        ("Гречневая каша с яблоком и корицей", "Паста с индейкой и томатами", "Запечённая курица и салат"),
        ("Смузи-боул с ягодами и семенами", "Плов с курицей и овощами", "Рыбные котлеты и овощи на пару"),
    ],
    "veg": [
        ("Овсянка с ягодами и орехами", "Киноа-боул с нутом и овощами", "Тёплый салат с фетой и авокадо"),
        ("Омлет с овощами и цельнозерновой тост", "Чечевичный суп и хлеб", "Творожная запеканка и салат"),
        ("Йогурт с гранолой и бананом", "Булгур с печёными овощами и фетой", "Овощное рагу с фасолью"),
        ("Сырники с ягодным соусом", "Паста с томатами и базиликом", "Тофу с овощами на пару"),
        ("Яйца пашот на авокадо-тосте", "Ризотто с грибами", "Салат с киноа, нутом и овощами"),
        ("Гречка с яблоком и корицей", "Овощное карри с рисом", "Запечённые овощи с моцареллой"),
        ("Смузи-боул с ягодами и семенами", "Фалафель с овощами и хумусом", "Шакшука с хлебом"),
    ],
}


def compute(quiz: dict) -> dict:
    g = 5 if quiz.get("gender") == "m" else -161
    try:
        age = int(float(quiz.get("age") or 32))
    except Exception:
        age = 32
    b = quiz.get("body") or {}
    try:
        w = float(b.get("weight") or 70); h = float(b.get("height") or 170)
    except Exception:
        w, h = 70.0, 170.0
    bmr = 10 * w + 6.25 * h - 5 * age + g
    af = {"sed": 1.2, "light": 1.375, "mod": 1.55, "high": 1.725}.get(quiz.get("activity"), 1.375)
    cal = bmr * af
    goal = quiz.get("goal")
    if goal == "lose":
        cal *= 0.80
    elif goal == "gain":
        cal *= 1.12
    # Нижний порог: НИКОГДА не рекомендуем экстремальный дефицит (риск здоровью + не
    # выполнимо). Медицинский минимум ~1200 ккал (ж) / 1500 ккал (м).
    floor = 1500 if quiz.get("gender") == "m" else 1200
    cal = max(cal, floor)
    cal = int(round(cal / 10) * 10)
    return {"cal": cal, "P": round(cal * 0.30 / 4), "F": round(cal * 0.30 / 9),
            "C": round(cal * 0.40 / 4), "goal": GOAL_TXT.get(goal, "твоей цели")}


def week(quiz: dict) -> list[dict]:
    veg = "nomeat" in (quiz.get("diet") or [])
    bank = BANK["veg"] if veg else BANK["omni"]
    c = compute(quiz)["cal"]
    kc = [round(c * 0.30), round(c * 0.40), round(c * 0.30)]
    out = []
    for i, (bf, ln, dn) in enumerate(bank):
        out.append({"day": DAYS[i], "meals": [("Завтрак", bf, kc[0]),
                                              ("Обед", ln, kc[1]), ("Ужин", dn, kc[2])]})
    return out


# ---------- HTML-письма ----------

_CSS_WRAP = ("font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;margin:0 auto;"
             "background:#FBF8F1;color:#20321F;padding:0 0 32px")
_GREEN, _GREEN_D, _MINT, _MUTED = "#16A34A", "#0E7A36", "#E7F8EC", "#6B7566"


def _head(title: str, sub: str) -> str:
    return (
        f"<div style='background:#fff;padding:26px 24px 22px;border-bottom:1px solid #EEE7D8'>"
        f"<div style='font-weight:800;font-size:20px'><span style='display:inline-block;width:11px;height:11px;"
        f"border-radius:50%;background:{_GREEN};margin-right:8px;vertical-align:middle'></span>NutriPlan</div></div>"
        f"<div style='padding:28px 24px 6px'><h1 style='margin:0;font-size:24px;letter-spacing:-.02em'>{title}</h1>"
        f"<p style='color:{_MUTED};font-size:15px;margin:8px 0 0'>{sub}</p></div>")


def _norm_card(p: dict) -> str:
    def m(v, lab):
        return (f"<td style='background:{_MINT};border-radius:12px;padding:10px;text-align:center'>"
                f"<div style='font-size:19px;font-weight:800;color:{_GREEN_D}'>{v}</div>"
                f"<div style='font-size:12px;color:{_MUTED}'>{lab}</div></td>")
    return (
        f"<div style='margin:16px 24px;background:#fff;border:1px solid #EEE7D8;border-radius:18px;padding:20px'>"
        f"<div style='font-size:40px;font-weight:800;color:{_GREEN_D};letter-spacing:-.02em'>{p['cal']}"
        f"<span style='font-size:14px;color:{_MUTED};font-weight:600'> ккал/день</span></div>"
        f"<div style='color:{_MUTED};font-size:14px;margin-bottom:14px'>твоя норма для {p['goal']} без голода</div>"
        f"<table width='100%' cellspacing='8' cellpadding='0'><tr>"
        f"{m(p['P'],'белки, г')}{m(p['F'],'жиры, г')}{m(p['C'],'углеводы, г')}</tr></table></div>")


def _day_block(d: dict) -> str:
    rows = ""
    for name, dish, kc in d["meals"]:
        rows += (f"<tr><td style='padding:8px 0;border-top:1px solid #EEE7D8'>"
                 f"<b style='font-size:14px'>{name}</b> — <span style='font-size:14px'>{dish}</span></td>"
                 f"<td style='padding:8px 0;border-top:1px solid #EEE7D8;text-align:right;white-space:nowrap;"
                 f"color:{_MUTED};font-size:13px'>{kc} ккал</td></tr>")
    return (f"<div style='margin:0 24px 12px'><div style='font-size:13px;font-weight:800;color:{_MUTED};"
            f"text-transform:uppercase;letter-spacing:.04em;margin-bottom:2px'>{d['day']}</div>"
            f"<table width='100%' cellspacing='0' cellpadding='0'>{rows}</table></div>")


def _cta(href: str, label: str) -> str:
    return (f"<div style='padding:22px 24px 6px'><a href='{href}' style='display:block;text-align:center;"
            f"background:{_GREEN};color:#fff;text-decoration:none;font-weight:800;font-size:16px;"
            f"padding:16px;border-radius:14px'>{label}</a></div>")


def _foot() -> str:
    return (f"<p style='color:{_MUTED};font-size:12px;text-align:center;padding:20px 24px 0;line-height:1.5'>"
            f"Материалы носят справочный характер и не являются медицинской услугой. "
            f"При заболеваниях проконсультируйтесь со специалистом.<br>Отписаться можно ответом на это письмо.</p>")


def lead_html(quiz: dict, plan_link: str = "") -> str:
    """Бесплатное письмо после квиза: норма + первый день + CTA на полный план."""
    p = compute(quiz)
    d1 = week(quiz)[0]
    cta = _cta(plan_link, "Получить полный план на 7 дней") if plan_link else ""
    return (f"<div style='{_CSS_WRAP}'>"
            + _head("Твой план готов", "Рассчитали норму под твоё тело, активность и вкусы")
            + _norm_card(p)
            + f"<div style='padding:6px 24px 0;font-weight:800;font-size:16px'>Пример дня</div>"
            + _day_block(d1)
            + cta + _foot() + "</div>")


# Письмо после оплаты строит menu_email_html (ниже) на РЕАЛЬНОМ LLM-плане.
# paid_html (генерил из статичного банка) удалён как мёртвый код — не путать шаблоны.


# ---------- Веб-страница плана (seed будущего PWA) + письмо-меню ----------

def _meal_card(m: dict, day: int = 0, slot: str = "", idx: int = 0) -> str:
    # В ключ отметки входит НОМЕР приёма в дне: слоты повторяются («Перекус» ×2),
    # и по ключу «день:слот» отметка на одном приёме помечала оба — день считался
    # выполненным раньше времени и калории второго приёма падали в «съедено».
    key = f"{day}:{idx}:{slot}"
    kc = m.get("kcal", "")
    try:
        kcnum = int(float(m.get("kcal") or 0))
    except Exception:
        kcnum = 0
    macros = ""
    if m.get("p") or m.get("c") or m.get("f"):
        macros = (f"<span class='mm'>Б {_e(m.get('p','?'))} · Ж {_e(m.get('f','?'))} · У {_e(m.get('c','?'))}</span>")

    def _num(v) -> int:
        try:
            return int(float(v or 0))
        except Exception:
            return 0
    # Числа БЖУ едут в data-атрибутах: плитка «набрано из нормы» складывает их по
    # ОТМЕЧЕННЫМ приёмам. Иначе она показывала бы норму как факт — то есть врала.
    pfc = f" data-p='{_num(m.get('p'))}' data-f='{_num(m.get('f'))}' data-c='{_num(m.get('c'))}'"
    ing = "".join(f"<li>{_e(i)}</li>" for i in (m.get("ingredients") or []))
    steps = "".join(f"<li>{_e(s)}</li>" for s in (m.get("steps") or []))
    details = ""
    if ing or steps:
        details = (f"<details><summary>Рецепт</summary>"
                   + (f"<div class='dh'>Ингредиенты</div><ul class='ing'>{ing}</ul>" if ing else "")
                   + (f"<div class='dh'>Приготовление</div><ol class='steps'>{steps}</ol>" if steps else "")
                   + "</details>")
    name = m.get("name", "")
    slug = dish_photos.slugify(name)
    slot_txt = _e(m.get("slot", ""))
    # Строка, а не карточка. Карточка несла название, КБЖУ, рецепт-гармошку и три
    # кнопки — на день это восемь экранов прокрутки, и «что я ем дальше» тонуло.
    # В строке остаётся ровно то, что читают на бегу: фото, название, приём,
    # калории и отметка. Всё остальное — на экране блюда.
    img = (f"<span class='mimg'><img loading='lazy' data-slug='{_e(slug)}' alt='' "
           f"src='/dish/{quote(slug)}?t={quote(name)}'></span>")
    open_btn = (f"<button class='mopen' data-k='{key}' aria-label='Открыть блюдо: {_e(name)}'>{img}"
                f"<span class='mtxt'><b class='mname'>{_e(name)}</b>"
                f"<span class='mmeta'>{slot_txt}</span></span>"
                f"<span class='mk'>{_e(kc)}</span></button>")
    tick = (f"<button class='tick' data-k='{key}' aria-label='Отметить «приготовил»: {_e(name)}'>"
            f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='3' "
            f"stroke-linecap='round' stroke-linejoin='round'><path d='M5 12.5 10 17.5 19 7'/></svg></button>")
    # Подробности лежат в самой строке, но скрыты: экран блюда клонирует их
    # отсюда. Так у страницы один источник правды — не надо гонять рецепт
    # отдельным запросом и синхронизировать две копии.
    hidden = (f"<div class='mhide' hidden data-slug='{_e(slug)}' data-name='{_e(name)}' "
              f"data-slot='{slot_txt}' data-kcal='{_e(kc)}'>{macros}{details}"
              f"<div class='mact'><button class='done' data-k='{key}'><span class='dc'></span>Приготовил</button>"
              f"<button class='swap' data-day='{day}' data-slot='{slot}' data-i='{idx}' data-k='{key}'>Заменить</button>"
              f"<button class='dislike' data-name='{_e(name)}' title='Не нравится — убрать из меню'>"
              f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
              f"<path d='M17 2H7.3a2 2 0 0 0-2 1.7l-1.3 8A2 2 0 0 0 6 14h4l-.7 3.3a2 2 0 0 0 3.5 1.6L17 14'/>"
              f"<path d='M17 2h2a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-2'/></svg></button></div></div>")
    return (f"<div class='meal' data-k='{key}' data-kc='{kcnum}'{pfc}>"
            f"{open_btn}{tick}{hidden}</div>")


_QTY_RE = re.compile(
    r"^(?P<name>.+?)[\s,]+(?P<q>\d+(?:[.,]\d+)?)\s*"
    r"(?P<u>кг|г|мл|л|шт|ст\.?\s?л\.?|ч\.?\s?л\.?|зубчик\w*|кусоч\w+|пучк\w*|уп\.?)"
    r"(?P<tail>\b.*)$", re.I)


def _split_qty(s: str) -> tuple[str, float | None, str, str]:
    """«морковь 500 г» → ('морковь', 500.0, 'г', ''). Без количества — (текст, None, '', '')."""
    m = _QTY_RE.match(str(s).strip())
    if not m:
        return str(s).strip(), None, "", ""
    try:
        q = float(m.group("q").replace(",", "."))
    except ValueError:
        return str(s).strip(), None, "", ""
    u = re.sub(r"\s+", " ", m.group("u").strip().lower())
    return m.group("name").strip(), q, u, (m.group("tail") or "").strip()


def _fmt_qty(q: float) -> str:
    return str(int(q)) if abs(q - round(q)) < 1e-6 else f"{q:.1f}".replace(".", ",")


def _shopping(sh: list, days: list | None = None) -> str:
    """Список покупок, по которому можно ходить по магазину.

    Было: статичная стена из 41 строки без единого элемента управления. С ней
    нельзя делать то, ради чего список нужен, — отмечать купленное; человек в
    магазине держит место в голове и сбивается.

    Стало: каждая позиция — переключатель, отметки живут локально по токену
    плана, сверху видно «куплено N из M».

    Количество считаем ПО ДНЯМ, а не берём готовую недельную строку: переключатель
    «Сегодня / Завтра / На 3 дня / На неделю» обязан менять и цифры тоже. Список
    «яблоки 7 шт» в режиме «сегодня» — это не фильтр, а враньё.
    Категорию берём из недельного списка от модели: раскладывать продукты по
    отделам магазина мы сами не умеем, а она уже разложила.
    """
    days = days or []
    # продукт (ключ) → категория, из недельного списка
    cat_of: dict[str, str] = {}
    for c in sh or []:
        for i in c.get("items") or []:
            nm, _q, _u, _t = _split_qty(i)
            cat_of.setdefault(nm.strip().lower(), c.get("cat", ""))
    # продукт → {день: количество}; ключ учитывает единицу, складывать «шт» с «г» нельзя
    agg: dict[tuple[str, str], dict] = {}
    for di, d in enumerate(days):
        for m in d.get("meals") or []:
            for raw in m.get("ingredients") or []:
                nm, q, u, tail = _split_qty(raw)
                if not nm:
                    continue
                key = (nm.strip().lower(), u)
                e = agg.setdefault(key, {"name": nm, "unit": u, "tail": tail, "per": {}})
                e["per"][di] = e["per"].get(di, 0) + (q if q is not None else 0)
    if not agg:
        # У блюд нет ингредиентов (так бывает у банк-заготовки, когда LLM молчала).
        # Тогда показываем недельный список как есть, без переключателя периодов:
        # раскладывать его по дням не из чего, а врать про «сегодня» нельзя.
        return _shopping_flat(sh or _shopping_from_days(days))
    # раскладываем по категориям недельного списка; чего там нет — в «Остальное»
    OTHER = "Остальное"

    def _head(s: str) -> str:
        # Сравниваем по началу главного слова: в списке «морковь», в рецепте
        # «моркови» — падежи не должны отправлять продукт в «Остальное».
        w = re.sub(r"[^\w\s]", " ", s).split()
        return (w[0][:5] if w else "")

    by_cat: dict[str, list] = {}
    for (nmk, _u), e in agg.items():
        cat = cat_of.get(nmk)
        if not cat:
            h = _head(nmk)
            cat = next((v for k, v in cat_of.items() if h and _head(k) == h), "")
        by_cat.setdefault(cat or OTHER, []).append(e)
    order = [c.get("cat", "") for c in (sh or []) if c.get("cat")] + [OTHER]
    cats, total = "", 0
    for cat in order:
        lst = by_cat.pop(cat, None)
        if not lst:
            continue
        items = ""
        for e in sorted(lst, key=lambda x: x["name"].lower()):
            total += 1
            per = json.dumps({str(k): v for k, v in e["per"].items()}, ensure_ascii=False)
            # Отметка «куплено» привязана к САМОМУ продукту (имя+единица), а не к
            # его номеру в категории. Позиционный ключ ci-ii переезжал на чужой
            # продукт от любой перетасовки списка — новое меню, другой порядок
            # категорий, — и человек уходил в магазин с галочкой на том, чего не
            # покупал. Имя+единица — тот же ключ, по которому строка агрегирована,
            # так что в пределах списка он уникален.
            sid = f"{e['name'].strip().lower()}|{e['unit']}"
            items += (f"<li><label class='si' data-q='{_e(per)}' data-u='{_e(e['unit'])}' "
                      f"data-n='{_e(e['name'])}' data-t='{_e(e['tail'])}'>"
                      f"<input type='checkbox' data-si='{_e(sid)}'>"
                      f"<span class='sb'></span><span class='st'></span></label></li>")
        cats += (f"<div class='cat'><div class='ct'>{_e(cat)}<span></span></div>"
                 f"<ul>{items}</ul></div>")
    if not cats:
        return ""
    # Периоды: покупают либо «на сегодня по дороге домой», либо закупом на неделю.
    # «Завтра» отдельно — под «что разморозить с вечера».
    segs = "".join(
        f"<button class='pseg{' on' if p == '7' else ''}' data-p='{p}'>{t}</button>"
        for p, t in (("1", "Сегодня"), ("t", "Завтра"), ("3", "3 дня"), ("7", "Неделя")))
    return (f"<section class='sec'><div class='shead'><h2>Список покупок</h2>"
            f"<button class='sclear' id='sclear' type='button'>Снять отметки</button></div>"
            f"<div class='psegs' id='shopseg'>{segs}</div>"
            f"<div class='sprog'><span id='sdone'>0</span> из <span id='stot'>{total}</span> — куплено</div>"
            f"<div class='shop' id='shop' data-total='{total}'>{cats}</div></section>")


def _shopping_flat(sh: list) -> str:
    """Недельный список без разбивки по дням — запасной вид для планов, у блюд
    которых нет ингредиентов. Раздел обещан на экране оплаты, и пропасть он не
    имеет права."""
    if not sh:
        return ""
    cats, total = "", 0
    seen: dict[str, int] = {}          # одинаковые строки в разных категориях — чтобы ключи не слиплись
    for c in sh:
        items = ""
        for i in c.get("items") or []:
            total += 1
            # Ключ — сама строка продукта, а не её номер: см. комментарий в
            # _shopping_html. Здесь строка приходит от LLM как есть, поэтому
            # повтор («лимон» в двух категориях) разводим суффиксом.
            base = str(i).strip().lower()
            n = seen[base] = seen.get(base, 0) + 1
            sid = base if n == 1 else f"{base}#{n}"
            items += (f"<li><label class='si'><input type='checkbox' data-si='{_e(sid)}'>"
                      f"<span class='sb'></span><span class='st'>{_e(i)}</span></label></li>")
        if not items:
            continue
        cats += (f"<div class='cat'><div class='ct'>{_e(c.get('cat',''))}"
                 f"<span>{len(c.get('items') or [])}</span></div><ul>{items}</ul></div>")
    if not cats:
        return ""
    return (f"<section class='sec'><div class='shead'><h2>Список покупок</h2>"
            f"<button class='sclear' id='sclear' type='button'>Снять отметки</button></div>"
            f"<div class='sprog'><span id='sdone'>0</span> из <span id='stot'>{total}</span> — куплено</div>"
            f"<div class='shop' id='shop' data-total='{total}'>{cats}</div></section>")


def _shopping_from_days(days: list) -> list:
    """Собрать список из ингредиентов блюд, если готового списка в плане нет.

    Список покупок обещан прямо на экране оплаты. Но приходит он только от LLM,
    и на банк-фолбэке (когда LLM не ответила) поле пустое — раздел ПРОПАДАЛ
    молча, и человек не получал того, за что заплатил. Здесь хотя бы сводим
    ингредиенты, если они есть.
    """
    seen: dict[str, str] = {}          # ключ в нижнем регистре → как показывать
    for d in days or []:
        for m in d.get("meals") or []:
            for i in m.get("ingredients") or []:
                k = str(i).strip()
                if k:
                    seen.setdefault(k.lower(), k)
    items = list(seen.values())
    if not items:
        return []
    # Без категорий: раскладывать продукты по отделам магазина мы здесь не умеем,
    # а выдумывать неверные категории хуже, чем один честный список.
    return [{"cat": "Всё на неделю", "items": sorted(items, key=str.lower)}]


def _tips(tips: list) -> str:
    if not tips:
        return ""
    li = "".join(f"<li><span class='c'></span><span>{_e(t)}</span></li>" for t in tips)
    return f"<section class='sec'><h2>Советы под тебя</h2><ul class='tips'>{li}</ul></section>"


_GOALS = {"lose": "Снижение веса", "keep": "Удержание веса", "gain": "Набор массы",
          "health": "Здоровое питание"}
# Коды из анкеты — те же, что читает фильтр в plan_ai._ALLERGEN_WORDS.
_DIET_RU = {"nuts": "без орехов", "nogluten": "без глютена", "nomeat": "без мяса",
            "nofish": "без рыбы", "nolact": "без лактозы"}


def _params(pl: dict, goal_code: str, water_goal: int) -> str:
    """Под что собран план. Раньше эти цифры были только на экране «Сегодня»,
    и человек не мог свериться, ту ли цель он вообще указал в анкете."""
    q = pl.get("quiz") or {}
    rows = []
    goal = _GOALS.get(goal_code)
    if goal:
        rows.append(("Цель", goal))
    if pl.get("cal"):
        rows.append(("Норма", f"{_e(pl.get('cal'))} ккал/день"))
    if pl.get("P") or pl.get("F") or pl.get("C"):
        rows.append(("БЖУ", f"{_e(pl.get('P','—'))} · {_e(pl.get('F','—'))} · {_e(pl.get('C','—'))} г"))
    rows.append(("Вода", f"{water_goal} стаканов в день"))
    if pl.get("days"):
        rows.append(("Приёмов в день", str(len((pl['days'][0].get('meals') or [])))))
    body = "".join(f"<div class='prow'><span>{k}</span><b>{v}</b></div>" for k, v in rows)
    # Аллергии и диета — отдельной строкой чипами: это единственный параметр,
    # который влияет на безопасность, и прятать его в общий список нельзя.
    al = [_DIET_RU.get(c, c) for c in (q.get("diet") or []) if c]
    if al:
        chips = "".join(f"<i>{_e(a)}</i>" for a in al)
        body += f"<div class='prow col'><span>Исключено по анкете</span><div class='chips'>{chips}</div></div>"
    return (f"<section class='sec'><h2>Мой план</h2>"
            f"<div class='prefcard params'>{body}</div></section>")


_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]


def _fmt_ru_date(iso: str) -> str:
    try:
        from datetime import datetime
        d = datetime.fromisoformat(iso)
        return f"{d.day} {_MONTHS[d.month - 1]}"
    except Exception:
        return ""


def _acct_html(sub, token: str) -> str:
    """Секция «Подписка» в ЛК: статус + отмена/отвязка карты (требование ЮKassa)."""
    if not sub:
        return ""
    if sub.get("status") == "active":
        nxt = _fmt_ru_date(sub.get("next", ""))
        amt = _e(sub.get("amount", "499"))
        has_card = bool(sub.get("payment_method_id") or sub.get("has_card"))
        card = ("<span class='cnote'>Карта привязана для автопродления</span>" if has_card
                else "<span class='cnote'>Карта не привязана — автосписаний не будет</span>")
        unbind = (f"<button class='unbindb' id='unbindCard' data-token='{_e(token)}'>Отвязать карту</button>"
                  if has_card else "")
        # При отвязанной карте не обещаем списание — доступ до конца периода, потом завершение.
        sline = (f"Следующее списание: <b>{nxt}</b> · {amt} ₽/мес" if has_card
                 else f"Автопродления не будет · доступ до <b>{nxt}</b>")
        return (f"<section class='sec acct'><h2>Подписка</h2>"
                f"<div class='subcard'><div class='sactive'>Активна</div>"
                f"<div class='sline'>{sline}</div>{card}"
                f"{unbind}"
                f"<button class='cancelb' id='cancelSub' data-token='{token}'>Отменить подписку</button>"
                f"<div class='cmsg' id='cmsg'></div></div></section>")
    return (f"<section class='sec acct'><h2>Подписка</h2>"
            f"<div class='subcard'><div class='scanceled'>Отменена</div>"
            f"<div class='sline'>Списаний больше не будет. Доступ сохраняется до конца оплаченного периода.</div>"
            f"</div></section>")


def _days_since(iso: str) -> int | None:
    """Сколько полных суток прошло с начала плана. None — даты нет или она битая."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        d = datetime.fromisoformat(str(iso))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except Exception:
        return None


def _renews_weeks(sub) -> bool:
    """Придёт ли следующая неделя сама. Не только у active: отменившему подписку cron
    довозит недели до конца ОПЛАЧЕННОГО периода (app.py, _period_paid), и до этой даты
    «план не продлевается» было бы прямым враньём — меню придёт и на почту, и сюда."""
    if not sub:
        return False
    st = sub.get("status")
    if st == "active":
        return True
    if st != "canceled":
        return False
    left = _days_since(sub.get("next") or "")   # дата платежа впереди → «прошло» отрицательное
    return left is not None and left < 0


def _weekover(ndays: int, renew_link: str) -> str:
    """Экран «неделя пройдена» для разового плана.

    Разовый план не создаёт подписку, а регенерацию гоняет cron только по
    подпискам — нового меню не будет НИКОГДА. Без этого блока на 8-й день человек
    открывает ту же неделю, где всё отмечено, и повода возвращаться нет.
    Текст не обещает автосборку: её здесь действительно не происходит.
    """
    return (f"<section class='wover'><div class='wobadge'>Неделя пройдена</div>"
            f"<h2>Твои 7 дней закончились</h2>"
            f"<p class='wosum'>Выполнено <b><span id='woDone'>0</span> из {ndays}</b> дней.</p>"
            f"<p class='wotxt'>План остаётся здесь: меню, рецепты и список покупок никуда не денутся — "
            f"по ним можно готовить дальше. Но новое меню он сам не соберёт: разовый план "
            f"рассчитан на одну неделю и не продлевается.</p>"
            f"<a class='wocta' href='{_e(renew_link)}'>Собрать новую неделю по подписке</a>"
            # Не пишем «придёт сюда же»: подписка оформляется через квиз, а он заводит
            # НОВЫЙ токен плана — ссылка на новую неделю будет другой, она в письме.
            f"<p class='wonote'>В подписке новое меню на 7 дней приходит каждую неделю — "
            f"письмом со ссылкой на обновлённый план.</p></section>")


def page_html(pl: dict, title: str = "Твой план питания", token: str = "", sub=None,
              renew_link: str = "/quiz") -> str:
    days = pl.get("days") or []
    q = pl.get("quiz") or {}
    try:
        start_w = float((q.get("body") or {}).get("weight") or 0)
    except Exception:
        start_w = 0.0
    goal_code = q.get("goal") or ""
    exclude = (q.get("exclude") or "").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    water_goal = max(6, min(12, round(start_w * 30 / 250))) if start_w else 8
    manifest = f"/app.webmanifest?t={token}" if token else "/app.webmanifest"
    acct = _acct_html(sub, token)
    # Ссылка на подписку — только если подписка есть. Разовому плану нечего там
    # показывать, а пустое окно раздражает сильнее отсутствующей ссылки.
    subs_link = "<a href='#' id='subsopen'>Подписка</a>" if acct else ""
    # ver/started пишет app.py при сохранении плана; у планов, созданных раньше, их нет —
    # тогда ведём себя как прежде. Фильтруем символы, потому что ver уезжает в JS-строку.
    ver = "".join(c for c in str(pl.get("ver") or "") if c.isalnum() or c in "-_.")
    week_over = ""
    if not _renews_weeks(sub):   # кому неделю пересоберёт cron — блок не показываем
        age = _days_since(pl.get("started") or "")
        if age is not None and age >= 7:
            week_over = _weekover(len(days), renew_link)
    # Две кнопки вместо ленты «1…7». Лента дублировала вкладку «Неделя», а на
    # экране «Сегодня» человек решает две задачи: что ем сейчас и что купить/
    # разморозить на завтра. Номер дня сам по себе ни о чём не говорит.
    # data-rel, а не data-d: какой день «сегодняшний», знает только клиент —
    # он считает его от даты старта плана.
    tabs = ("<button class='tab on' data-rel='0'>Сегодня</button>"
            "<button class='tab' data-rel='1' hidden>Завтра</button>")
    # Строки недели: что в этот день, сколько ккал. Обзор без открытия дня.
    week_rows = ""
    for i, d in enumerate(days):
        ms = d.get("meals") or []
        names = " · ".join(_e(m.get("name", "")) for m in ms[:3])
        tot = sum(int(m.get("kcal") or 0) for m in ms)
        week_rows += (f"<button class='drow' data-d='{i}'>"
                      f"<span class='dl'><b>{_day_label(i)}<em class='dnow' hidden> · сегодня</em></b>"
                      f"<span>{names}</span></span>"
                      f"<span class='dk'>{tot} ккал</span></button>")
    panels = ""
    for i, d in enumerate(days):
        meals = "".join(_meal_card(m, i, m.get("slot", ""), j)
                        for j, m in enumerate(d.get("meals") or []))
        tot = sum(int(m.get("kcal") or 0) for m in (d.get("meals") or []))
        # Полоса и «съедено» переехали в карточку нормы наверху — там же, где
        # число. Две шкалы про одно и то же на одном экране только спорили друг
        # с другом. data-tot остаётся: по нему карточка считает остаток.
        panels += (f"<div class='panel{" on" if i==0 else ""}' data-d='{i}' data-tot='{tot}'>"
                   f"<div class='dtitle'>{_day_label(i)} <span>{tot} ккал</span></div>"
                   f"{meals}</div>")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title} · NutriPlan</title>
<link rel="manifest" href="{manifest}">
<meta name="theme-color" content="#16A34A">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="NutriPlan">
<link rel="apple-touch-icon" href="/assets/icon-192.png">
<style>
/* --wtr — вода. Отдельный токен, а не разовый цвет в правиле: вода отмечается
   в двух местах (стаканы и полоса прогресса дня), и они обязаны совпадать. */
/* ═══ Оформление ═══════════════════════════════════════════════════════════
   Приложение приведено к виду прототипа v5 «светлое стекло»: свои шрифты с
   кириллицей, крупная типографика, полупрозрачные карточки со светящейся
   кромкой поверх цветного свечения, всё скруглено.

   Системный шрифт и белые карточки одного размера — главная причина, по которой
   интерфейс выглядел черновиком, как ни расставляй блоки. Onest на текст,
   Unbounded на числа; самохостятся, только подмножества cyrillic+latin.        */
@font-face{{font-family:Onest;font-style:normal;font-weight:400 800;font-display:swap;
  src:url(/assets/onest-cyrillic.woff2) format('woff2');
  unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}}
@font-face{{font-family:Onest;font-style:normal;font-weight:400 800;font-display:swap;
  src:url(/assets/onest-latin.woff2) format('woff2');
  unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+2000-206F,U+2070-209F,U+20AC,U+2122,U+2212}}
@font-face{{font-family:Unbounded;font-style:normal;font-weight:600 900;font-display:swap;
  src:url(/assets/unbounded-cyrillic.woff2) format('woff2');
  unicode-range:U+0301,U+0400-045F,U+0490-0491,U+04B0-04B1,U+2116}}
@font-face{{font-family:Unbounded;font-style:normal;font-weight:600 900;font-display:swap;
  src:url(/assets/unbounded-latin.woff2) format('woff2');
  unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+2000-206F,U+20AC,U+2122,U+2212}}

:root{{--g:#1E9150;--gd:#136B39;--soft:#DFF3E5;--ink:#1B2A17;--ink-2:#4C5C46;
  --muted:#8B9584;--bg:#F7F2E8;--line:#E6DECD;--card:#FFFDF8;
  --accent:#E9682F;--wtr:#3FBEF0;--wtr-soft:#E0F2FE;
  --glass:color-mix(in srgb,#fff 62%,transparent);
  --glass-edge:color-mix(in srgb,#fff 92%,transparent);
  --glass-line:color-mix(in srgb,var(--ink) 9%,transparent);
  --sh-1:0 2px 6px -2px rgba(30,50,25,.10);
  --sh-2:0 18px 36px -26px rgba(30,50,25,.42);
  --rx:30px;--rl:22px;--rm:16px;--rs:12px}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--ink);font-family:Onest,-apple-system,BlinkMacSystemFont,sans-serif;
  -webkit-font-smoothing:antialiased}}
/* Свечение под контентом — четыре пятна, не больше: каждое это большой размытый
   слой, и на телефоне за него платят батареей. */
.aura{{position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(38% 26% at 12% 6%,  color-mix(in srgb,var(--g) 24%,transparent), transparent 70%),
    radial-gradient(42% 28% at 94% 18%, color-mix(in srgb,var(--accent) 16%,transparent), transparent 72%),
    radial-gradient(48% 30% at 4% 62%,  color-mix(in srgb,var(--soft) 90%,transparent), transparent 74%),
    radial-gradient(56% 34% at 84% 92%, color-mix(in srgb,var(--wtr) 14%,transparent), transparent 72%);
  filter:blur(40px)}}
/* Ощущение стекла даёт не прозрачность, а СВЕТЯЩАЯСЯ КРОМКА сверху: без неё
   выходит просто мутный прямоугольник. */
.norm,.streakc,.water,.meal,.wcard,.prefcard,.subcard,.si,.drow,.tips li,.wover,.pcell{{
  background:var(--glass)!important;
  -webkit-backdrop-filter:blur(22px) saturate(165%);backdrop-filter:blur(22px) saturate(165%);
  border:1px solid var(--glass-line)!important;
  box-shadow:inset 0 1px 0 var(--glass-edge),inset 0 -1px 0 rgba(30,50,25,.05),var(--sh-2)!important}}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){{
  .norm,.streakc,.water,.meal,.wcard,.prefcard,.subcard,.si,.drow,.tips li,.wover,.pcell{{background:var(--card)!important}}
}}
@media (prefers-reduced-transparency:reduce){{
  .aura{{display:none}}
  .norm,.streakc,.water,.meal,.wcard,.prefcard,.subcard,.si,.drow,.tips li,.wover,.pcell{{
    background:var(--card)!important;backdrop-filter:none;-webkit-backdrop-filter:none}}
}}
.wrap{{max-width:560px;margin:0 auto;padding:0 18px 60px}}
header{{position:sticky;top:0;background:color-mix(in srgb,var(--bg) 86%,transparent);
  -webkit-backdrop-filter:blur(14px) saturate(150%);backdrop-filter:blur(14px) saturate(150%);
  padding:14px 0;z-index:5;border-bottom:1px solid var(--glass-line)}}
.brand{{max-width:560px;margin:0 auto;padding:0 18px;font-weight:800;font-size:19px;display:flex;align-items:center}}
.brand .dot{{display:inline-block;width:11px;height:11px;border-radius:50%;background:var(--g);margin-right:8px}}
.ins{{margin-left:auto;border:none;background:var(--g);color:#fff;font-weight:700;font-size:13px;padding:8px 14px;border-radius:99px;cursor:pointer}}
.ins[hidden]{{display:none}}
.brand{{font-family:Unbounded;font-weight:700;letter-spacing:-.03em}}
h1{{font-family:Unbounded;font-weight:800;font-size:30px;letter-spacing:-.05em;line-height:.98;
  margin:20px 0 8px}}
.lead{{color:var(--ink-2);font-size:14.5px}}
.norm{{border-radius:var(--rx);padding:20px 21px 18px;margin-top:16px}}
.norm .cap{{font-size:11px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;
  color:var(--muted)}}
.norm .big{{font-family:Unbounded;font-weight:800;font-size:54px;color:var(--gd);
  letter-spacing:-.055em;line-height:.9;margin-top:9px}}
.norm .big i{{font-style:normal;font-family:Onest;font-size:16px;color:var(--muted);font-weight:600;
  letter-spacing:-.01em;margin-left:9px}}
.norm .sub{{font-size:13px;color:var(--muted);margin-top:8px}}
/* Шкала сегментами по приёмам, а не сплошная: в дне пять приёмов, и «сколько
   осталось» человек считает именно ими, а не процентами. */
.norm .track{{display:flex;gap:5px;margin-top:16px}}
.norm .track i{{flex:1;height:6px;border-radius:99px;
  background:color-mix(in srgb,var(--ink) 8%,transparent)}}
.norm .track i.on{{background:var(--g)}}
.norm .track i.over{{background:#E0912B}}
/* Плитки БЖУ — те же, что на экране оплаты, вместе с рисованными иконками:
   оплата и приложение должны читаться как один продукт. Подложки у плиток нет —
   карточка нормы уже задаёт границу, а место уходит числам. */
.macros{{display:flex;gap:14px;margin-top:16px;padding-top:14px;border-top:1px solid var(--glass-line)}}
.macros > div{{flex:1;min-width:0;background:none;border:0;padding:0;text-align:left}}
.macros .mrow{{display:flex;align-items:center;gap:8px;justify-content:flex-start}}
.macros .mbar{{height:4px;border-radius:99px;margin-top:9px;
  background:color-mix(in srgb,var(--ink) 8%,transparent)}}
.macros .mbar i{{display:block;height:100%;width:0;border-radius:99px;background:var(--g);
  transition:width .35s ease}}
.macros b u{{text-decoration:none}}
.macros .mic{{width:30px;height:30px;flex:0 0 auto}}
.macros .mic img{{width:100%;height:100%;display:block}}
.macros .mtx{{min-width:0}}
.macros b i{{font-style:normal;font-family:Onest;font-size:11.5px;font-weight:700;
  color:var(--muted);margin-left:1px;letter-spacing:0}}
.macros b{{display:block;font-family:Unbounded;font-weight:700;font-size:16px;color:var(--ink);
  letter-spacing:-.045em;line-height:1.05;white-space:nowrap}}
.macros span{{display:block;font-size:10px;color:var(--muted);margin-top:3px;white-space:nowrap}}
/* Без липкой подложки: она нужна была ленте «1…7», которая при прокрутке
   уезжала под контент. Двум кнопкам прилипать незачем, а полоса блюра поперёк
   экрана перебивала свечение. */
.tabs{{display:flex;gap:8px;margin:22px 0 14px}}
.tab{{flex:0 0 auto;border:1px solid var(--glass-line);color:var(--muted);font-weight:700;font-size:14px;
  padding:9px 15px;border-radius:99px;cursor:pointer;font-family:inherit;
  background:var(--glass);-webkit-backdrop-filter:blur(18px) saturate(160%);backdrop-filter:blur(18px) saturate(160%);
  box-shadow:inset 0 1px 0 var(--glass-edge),var(--sh-1)}}
.tab.on{{background:var(--g);color:#fff;border-color:var(--g);box-shadow:var(--sh-1)}}
.panel{{display:none}}.panel.on{{display:block;animation:in .3s ease}}
@keyframes in{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
.dtitle{{font-weight:700;font-size:11px;margin:6px 4px 12px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted)}}
.dtitle span{{color:var(--gd)}}
/* Приём пищи — строка-пилюля. Слева фото, дальше название и приём, справа
   калории и отметка. Всё, что не читают на бегу (КБЖУ, рецепт, кнопки), уехало
   на экран блюда: в карточке они превращали день в восемь экранов прокрутки. */
.meal{{border-radius:var(--rl);padding:10px 12px 10px 10px;margin-bottom:10px;
  display:flex;align-items:center;gap:10px}}
.mopen{{flex:1;min-width:0;display:flex;align-items:center;gap:12px;background:none;border:0;
  padding:0;font:inherit;color:inherit;text-align:left;cursor:pointer}}
.mimg{{width:52px;height:52px;flex:0 0 auto;border-radius:var(--rm);background:var(--soft);
  display:block;overflow:hidden;position:relative}}
.mimg img{{width:100%;height:100%;object-fit:cover;display:block}}
.mtxt{{flex:1;min-width:0}}
.mname{{display:block;font-size:14.5px;font-weight:600;line-height:1.25}}
.mmeta{{display:block;font-size:11.5px;color:var(--muted);margin-top:2px}}
.mk{{font-family:Unbounded;font-weight:700;font-size:13.5px;letter-spacing:-.035em;
  color:var(--ink-2);white-space:nowrap}}
/* Отметка — своя кнопка, а не часть строки: тап по строке открывает блюдо, и
   промахнуться между «посмотреть» и «съедено» нельзя. 34px — палец попадает. */
.tick{{width:34px;height:34px;flex:0 0 auto;border:0;border-radius:12px;cursor:pointer;padding:0;
  background:color-mix(in srgb,var(--ink) 6%,transparent);color:transparent;
  display:flex;align-items:center;justify-content:center;transition:.15s}}
.tick svg{{width:17px;height:17px;display:block}}
.tick:focus-visible{{outline:2px solid var(--gd);outline-offset:2px}}
.meal.on .tick{{background:var(--g);color:#fff}}
.meal.on{{opacity:.55}}
.meal.on .mname{{text-decoration:line-through}}

/* Фото на весь экран. Открывается тапом по марке в списке. */
.lb{{position:fixed;inset:0;z-index:90;display:none;align-items:center;justify-content:center;
  background:rgba(20,28,18,.82);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
  padding:20px;cursor:zoom-out}}
.lb.on{{display:flex}}
.lb figure{{max-width:min(520px,100%);width:100%;margin:0}}
.lb img{{width:100%;height:auto;display:block;border-radius:18px;background:var(--soft);
  box-shadow:0 30px 60px -20px rgba(0,0,0,.6)}}
.lb figcaption{{color:#fff;font-weight:700;font-size:15px;text-align:center;margin-top:12px}}
.lb .x{{position:absolute;top:12px;right:12px;width:40px;height:40px;border-radius:50%;border:none;
  background:rgba(255,255,255,.16);color:#fff;font-size:22px;line-height:1;cursor:pointer}}
.mm{{font-size:12px;color:var(--muted)}}
.dh{{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;
  letter-spacing:.11em;margin:24px 0 11px}}
.ing,.steps{{padding-left:18px;font-size:14px;line-height:1.6}}.steps li{{margin-bottom:4px}}

/* ── экран блюда ────────────────────────────────────────────────────────────
   Отдельный ЭКРАН, а не гармошка в списке: тут фото, КБЖУ, состав, шаги и все
   действия — на телефоне такому нужна вся высота. Возврат кнопкой и системным
   «назад» (экран заводится в историю), иначе жест уводил бы из приложения. */
/* z-index выше просмотра дня: блюдо открывается ПОВЕРХ него, а не вместо. */
.dishv{{position:fixed;inset:0;z-index:70;background:var(--bg);overflow:auto;display:none}}
.dishv.on{{display:block}}
/* Открыто из чужого дня — «Приготовил» прячем: съеденное отмечают в тот день,
   когда едят. Заменить блюдо при этом можно, ради этого сюда и заходят. */
.dishv.fromday .done{{display:none}}
.dishv.fromday .swap{{flex:1}}
.swapday{{width:100%;border:1.5px solid var(--line);background:var(--card);color:var(--gd);
  font-family:inherit;font-weight:700;font-size:15px;padding:14px;border-radius:var(--rl);
  cursor:pointer;margin-bottom:12px}}
.swapday:disabled{{opacity:.6}}
.dvbody .dshot{{border-radius:var(--rx);overflow:hidden;box-shadow:var(--sh-2);cursor:zoom-in;
  display:block;padding:0;border:0;background:none;width:100%}}
.dvbody .dshot img{{width:100%;height:250px;object-fit:cover;display:block;background:var(--soft)}}
.dvbody h2{{font-family:Unbounded;font-weight:800;font-size:26px;letter-spacing:-.045em;
  line-height:1.05;margin:20px 0 0}}
.dvbody .dmeta{{color:var(--muted);font-size:13.5px;margin-top:8px}}
.dkcal{{display:flex;gap:10px;margin-top:16px}}
.dkcal div{{flex:1;border-radius:var(--rm);padding:12px 8px;text-align:center;
  background:color-mix(in srgb,var(--ink) 4%,transparent)}}
.dkcal b{{display:block;font-family:Unbounded;font-weight:700;font-size:18px;letter-spacing:-.045em}}
.dkcal b i{{font-style:normal;font-family:Onest;font-size:11px;font-weight:700;color:var(--muted);
  letter-spacing:0;margin-left:1px}}
.dkcal span{{display:block;font-size:9.5px;font-weight:700;letter-spacing:.07em;
  text-transform:uppercase;color:var(--muted);margin-top:5px}}
/* Действия прибиты к низу окна: до них не нужно доскроллить рецепт. Обратная
   сторона — они закрывают последние строки, поэтому телу нужен запас ровно на
   высоту панели: длинный рецепт обрывался на «3. Добавить курицу…».
   Правило именно для .dishv: в просмотре дня панели нет, и лишний экран пустоты
   там ни к чему. */
.dishv .dvbody{{padding-bottom:calc(120px + env(safe-area-inset-bottom))}}
.dact{{position:fixed;left:0;right:0;bottom:0;padding:12px 18px calc(14px + env(safe-area-inset-bottom));
  background:linear-gradient(to top,var(--bg) 68%,transparent);z-index:2}}
.dact .mact{{max-width:560px;margin:0 auto}}
.sec{{margin-top:30px}}
.sec h2{{font-family:Unbounded;font-size:20px;font-weight:700;letter-spacing:-.035em;margin-bottom:12px}}
.shop{{display:flex;flex-direction:column;gap:6px}}
/* Категория — не карточка внутри карточки: заголовок группы и под ним пилюли.
   Вложенное стекло в стекле читалось как грязь. */
.cat{{background:none;border:0;padding:0}}
.ct{{display:flex;align-items:center;gap:9px;font-size:12.5px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin:14px 4px 9px}}
.ct span{{margin-left:auto;letter-spacing:0;font-size:11.5px}}
.cat ul{{list-style:none;padding:0;font-size:14px;display:flex;flex-direction:column;gap:8px}}
/* Позиция — переключатель, а не строчка текста: список нужен в магазине, где
   единственное действие — отметить купленное. Цель нажатия во всю ширину, чтобы
   попадать пальцем не глядя. */
.si{{display:flex;align-items:center;gap:12px;padding:13px 15px;cursor:pointer;line-height:1.35;
  border-radius:var(--rl)}}
.si input{{position:absolute;opacity:0;width:0;height:0}}
.si .sb{{width:21px;height:21px;flex:0 0 auto;border:2px solid var(--line);border-radius:6px;
  margin-top:1px;position:relative;transition:.14s}}
.si input:checked+.sb{{background:var(--g);border-color:var(--g)}}
.si input:checked+.sb::after{{content:"";position:absolute;left:6px;top:2px;width:6px;height:11px;
  border:2px solid #fff;border-top:0;border-left:0;transform:rotate(45deg)}}
.si input:focus-visible+.sb{{outline:2px solid var(--gd);outline-offset:2px}}
.si input:checked~.st{{color:var(--muted);text-decoration:line-through}}
.shead{{display:flex;align-items:baseline;justify-content:space-between;gap:12px}}
.sclear{{border:none;background:none;color:var(--muted);font-size:13px;font-weight:700;
  text-decoration:underline;text-underline-offset:2px;cursor:pointer;padding:0;font-family:inherit}}
.sprog{{color:var(--muted);font-size:13px;font-weight:700;margin:-2px 0 12px}}
/* Период закупки. Сегментами, а не выпадашкой: вариантов четыре, и все они
   должны быть видны сразу — выбор делают у полки, одной рукой. */
.psegs{{display:flex;gap:6px;margin:12px 0 10px;overflow-x:auto}}
.pseg{{flex:1 0 auto;border:1px solid var(--glass-line);background:var(--glass);color:var(--muted);
  font:inherit;font-weight:700;font-size:13px;padding:9px 12px;border-radius:99px;cursor:pointer;
  -webkit-backdrop-filter:blur(18px) saturate(160%);backdrop-filter:blur(18px) saturate(160%);
  box-shadow:inset 0 1px 0 var(--glass-edge),var(--sh-1);white-space:nowrap}}
.pseg.on{{background:var(--g);color:#fff;border-color:var(--g);box-shadow:var(--sh-1)}}
.si[hidden],.cat[hidden]{{display:none}}
.snone{{color:var(--muted);font-size:14px;padding:14px 4px}}
.sprog.all{{color:var(--gd)}}
.tips{{list-style:none;display:flex;flex-direction:column;gap:12px}}
.tips li{{display:flex;gap:10px;font-size:14.5px;border-radius:var(--rl);padding:13px 15px}}
.tips .c{{width:8px;height:8px;border-radius:50%;background:var(--g);flex:0 0 auto;margin-top:7px}}
.subcard{{border-radius:var(--rx);padding:18px}}
.sactive{{display:inline-block;background:var(--soft);color:var(--gd);font-weight:800;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:5px 12px;border-radius:99px}}
.scanceled{{display:inline-block;background:#f3f4f6;color:#6b7280;font-weight:800;font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:5px 12px;border-radius:99px}}
.sline{{margin-top:12px;font-size:15px}}
.cnote{{display:block;color:var(--muted);font-size:13px;margin-top:4px}}
.unbindb{{margin-top:16px;width:100%;border:1.5px solid var(--g);background:var(--card);color:var(--gd);font-weight:700;font-size:14px;padding:12px;border-radius:12px;cursor:pointer}}
.unbindb:hover{{background:var(--soft)}}.unbindb:disabled{{opacity:.5}}
.cancelb{{margin-top:10px;width:100%;border:1.5px solid var(--line);background:var(--card);color:#b91c1c;font-weight:700;font-size:14px;padding:12px;border-radius:12px;cursor:pointer}}
.cancelb:hover{{border-color:#b91c1c}}.cancelb:disabled{{opacity:.5}}
.cmsg{{margin-top:12px;font-size:14px;color:var(--gd);font-weight:700;display:none}}.cmsg.s{{display:block}}
.plegal{{margin-top:40px;padding-top:22px;border-top:1px solid var(--line);text-align:center;font-size:13px;color:var(--muted)}}
.plegal .plinks a{{color:var(--muted);margin:0 8px;text-decoration:underline;text-underline-offset:2px}}
.plegal .preq{{margin-top:10px}}.plegal .preq a{{color:var(--muted)}}
/* Юридическая оговорка: мелкая, но читаемая. 11.5px и обычный muted — это
   сноска, а не скрытый текст; невидимая оговорка юридически бесполезна. */
.plegal .pdisc{{margin-top:12px;font-size:11.5px;line-height:1.45;max-width:52ch;
  margin-left:auto;margin-right:auto;color:var(--muted)}}
/* «Неделя пройдена». Спокойный блок, а не перекрывающее окно: план под ним
   остаётся рабочим, человек имеет право просто готовить дальше. */
.wover{{border-radius:var(--rx);padding:20px;margin-top:18px}}
.wobadge{{display:inline-block;background:var(--soft);color:var(--gd);font-weight:800;font-size:12px;
  text-transform:uppercase;letter-spacing:.04em;padding:5px 12px;border-radius:99px}}
.wover h2{{font-size:21px;font-weight:800;letter-spacing:-.01em;margin:12px 0 6px}}
.wosum{{font-size:15px}}.wosum b{{color:var(--gd)}}
.wotxt{{font-size:14px;color:var(--muted);line-height:1.5;margin-top:10px}}
.wocta{{display:block;text-align:center;margin-top:16px;background:var(--g);color:#fff;text-decoration:none;
  font-weight:800;font-size:15px;padding:14px;border-radius:14px}}
.wonote{{font-size:13px;color:var(--muted);line-height:1.45;margin-top:10px;text-align:center}}
.streakc{{border-radius:var(--rl);padding:14px 15px;margin-top:16px}}
.ssub{{font-size:13px;color:var(--muted);margin-top:1px}}
.tab.complete:not(.on){{border-color:var(--g);color:var(--gd)}}
.tab.complete::before{{content:"✓ "}}
.mact{{display:flex;gap:8px;margin-top:12px}}
.done{{flex:1;border:1.5px solid var(--line);background:var(--card);color:var(--muted);font-weight:700;
  font-size:15px;padding:15px;border-radius:var(--rl);cursor:pointer;display:flex;
  align-items:center;justify-content:center;gap:9px;font-family:inherit}}
.swap{{flex:0 0 auto;border:1.5px solid var(--line);background:var(--card);color:var(--muted);font-weight:700;
  font-size:14px;padding:11px 16px;border-radius:var(--rl);cursor:pointer;font-family:inherit}}
.swap:hover{{border-color:var(--g);color:var(--gd)}}.swap:disabled{{opacity:.5}}
/* «Не нравится». У кнопки не было НИ ОДНОГО правила: инлайновый svg без
   размеров сплющивал её в вертикальную чёрточку справа от «Заменить» — на
   экране это читалось как случайный артефакт вёрстки, а не как кнопка.
   Делаем квадратной под высоту соседей и задаём размер иконке. */
.dislike{{flex:0 0 auto;width:52px;border:1.5px solid var(--line);background:var(--card);
  color:var(--muted);border-radius:var(--rl);cursor:pointer;display:flex;align-items:center;
  justify-content:center;padding:0;transition:.15s}}
.dislike svg{{width:19px;height:19px;display:block}}
.dislike:hover{{border-color:#DC2626;color:#DC2626}}
.dislike:disabled{{opacity:.5}}
/* Подтверждение вторым нажатием должно быть ВИДНО на телефоне. Раньше первый
   тап менял только title — то есть подсказку, которая на тач-экране не
   показывается вообще, — и человек не понимал, нажалось ли что-нибудь. */
.dislike.armed{{border-color:#DC2626;color:#DC2626;background:#FEF2F2}}
.mact .hintx{{flex:0 0 100%;color:#DC2626;font-size:12.5px;font-weight:600;margin-top:-2px;display:none}}
.mact .hintx.show{{display:block}}
.mact{{flex-wrap:wrap}}
.done .dc{{width:18px;height:18px;border-radius:50%;border:2px solid var(--line);flex:0 0 auto;position:relative}}
/* Состояние живёт на самой кнопке, а не на родительской карточке: на экране
   блюда кнопка вынута из строки, и селектор `.meal.on .done` там не сработал бы —
   человек отмечал «Приготовил», а кнопка оставалась серой. */
.done.on{{border-color:var(--g);background:var(--soft);color:var(--gd)}}
.done.on .dc{{background:var(--g);border-color:var(--g)}}
.done.on .dc::after{{content:"";position:absolute;left:4px;top:1px;width:6px;height:10px;border:2px solid #fff;border-top:0;border-left:0;transform:rotate(45deg)}}
.done.on::after{{content:" ✓"}}
/* ── экраны и нижняя навигация ────────────────────────────────────────────
   Страница была одним свитком на 5292 px (восемь экранов) со всеми 35 блюдами
   недели сразу и без навигации. Те же разделы разложены по четырём вкладкам;
   разметка и обработчики не тронуты — переставлены только контейнеры. */
.scr{{display:none}}
.scr.on{{display:block}}
body{{padding-bottom:104px}}
.bnav{{position:fixed;left:14px;right:14px;bottom:calc(13px + env(safe-area-inset-bottom));z-index:40;
  max-width:536px;margin:0 auto;display:flex;padding:8px 7px;border-radius:26px;
  background:var(--glass);border:1px solid var(--glass-line);
  -webkit-backdrop-filter:blur(24px) saturate(170%);backdrop-filter:blur(24px) saturate(170%);
  box-shadow:inset 0 1px 0 var(--glass-edge),0 22px 44px -24px rgba(30,50,25,.5)}}
.bnav button{{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;border:none;
  background:none;color:var(--muted);font:inherit;font-size:10px;font-weight:700;
  padding:8px 0 6px;border-radius:19px;cursor:pointer}}
.bnav button svg{{width:22px;height:22px;display:block}}
.bnav button.on{{color:var(--gd);background:var(--soft)}}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){{
  .bnav,.tab{{background:var(--card)}}
}}
/* Две плитки в ряд: серия и вода — про одно и то же (привычки), и по одной на
   строку они занимали пол-экрана. */
.tiles{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}}
.tiles .streakc,.tiles .water{{margin-top:0;padding:13px 14px}}
/* В половину ширины прежнее содержимое не влезало: подпись серии ломалась на
   четыре строки, «Вода сегодня» на две, стаканы в три ряда. Ужимаем именно то,
   что можно ужать без потери смысла. */
.tiles .streakc{{display:block}}
.tiles .streakc .ssub{{display:none}}          /* подсказку даёт онбординг */
/* Обе плитки — одна сетка: подпись / ряд значков / число. Без неё ряды в
   соседних плитках вставали на разной высоте. */
.tiles .streakc,.tiles .water{{display:grid;grid-template-rows:auto 26px;gap:10px;align-content:start}}
.tiles .wtop{{display:block;font-size:13px}}
.tiles .wtop b{{display:block;font-size:11px;font-weight:800;letter-spacing:.08em;
  text-transform:uppercase;color:var(--muted)}}
.tiles .wtop #wnum,.tiles .wtop #prog{{display:block;font-family:Unbounded;font-size:24px;
  font-weight:700;letter-spacing:-.045em;color:var(--ink);margin-top:6px}}
.tiles .water .wcups{{flex-wrap:nowrap;gap:4px;margin-top:0;align-items:flex-end}}
.tiles .water .cup{{width:auto;flex:1;min-width:0;height:22px}}
/* Огонёк многоцветный, перекрасить через currentColor нельзя — незакрытые дни
   гасим фильтром. */
.flames{{display:flex;gap:3px;align-items:flex-end}}
.flames span{{height:22px;display:block;flex:0 0 auto}}
.flames img{{height:100%;width:auto;display:block}}
.flames span.off img{{filter:grayscale(1) opacity(.28)}}

/* Неделя */
.days{{display:flex;flex-direction:column;gap:9px;margin-top:16px}}
.drow{{display:flex;align-items:center;gap:12px;width:100%;text-align:left;font:inherit;
  border-radius:var(--rl);padding:13px 15px;cursor:pointer;color:inherit}}
.drow .dl{{flex:1;min-width:0}}
.drow .dl b{{display:block;font-size:14.5px;font-weight:600;line-height:1.25}}
.drow .dnow{{font-style:normal;font-weight:700;color:var(--gd)}}
.drow .dnow[hidden]{{display:none}}
.drow .dl span{{display:block;font-size:11.5px;color:var(--muted);margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.drow .dk{{font-family:Unbounded;font-size:13.5px;font-weight:700;letter-spacing:-.035em;
  color:var(--ink-2);white-space:nowrap}}

/* Просмотр дня */
.dayview{{position:fixed;inset:0;z-index:60;background:var(--bg);overflow:auto;display:none}}
.dayview.on{{display:block}}
.dvtop{{position:sticky;top:0;z-index:2;display:flex;align-items:center;gap:12px;padding:12px 18px;
  background:color-mix(in srgb,var(--bg) 88%,transparent);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px)}}
.dvtop button{{width:38px;height:38px;border-radius:14px;border:1px solid var(--glass-line);
  background:var(--glass);color:var(--ink);display:flex;align-items:center;justify-content:center;
  cursor:pointer;flex:0 0 auto;
  -webkit-backdrop-filter:blur(18px) saturate(160%);backdrop-filter:blur(18px) saturate(160%);
  box-shadow:inset 0 1px 0 var(--glass-edge),var(--sh-1)}}
.dvtop button svg{{width:19px;height:19px;display:block}}
.dvtop b{{font-family:Unbounded;font-size:15px;font-weight:700;letter-spacing:-.035em}}
.dvbody{{max-width:560px;margin:0 auto;padding:4px 18px 40px}}
.dvnote{{font-size:12.5px;color:var(--muted);margin:2px 0 14px}}
/* Отдельной полосы калорий под вкладками больше нет: та же шкала теперь в
   карточке наверху, рядом с числом. Две шкалы про одно и то же спорили. */
.water{{border-radius:var(--rl);padding:14px 15px;margin-top:12px}}
.wtop{{display:flex;justify-content:space-between;align-items:center;font-size:15px;font-weight:800}}
.wtop #wnum{{color:var(--muted);font-weight:700;font-size:13px}}
.wcups{{display:flex;gap:6px;flex-wrap:wrap;margin-top:11px}}
.cup{{width:24px;height:28px;padding:0;border:none;background:none;cursor:pointer;color:var(--line);transition:color .15s}}
.cup svg{{width:100%;height:100%;display:block}}
/* Выпитый стакан — синий, а не зелёный: зелёным на этом экране помечено
   выполненное по еде («Приготовил», серия дней), и вода в том же цвете сливалась
   с ним в одну шкалу. Синий читается как вода без подписи. */
.cup.f{{color:var(--wtr)}}
.wcard{{border-radius:var(--rx);padding:18px}}
.wrow{{display:flex;justify-content:space-between;align-items:flex-end}}
.wbig{{font-family:Unbounded;font-size:32px;font-weight:700;color:var(--gd);letter-spacing:-.05em}}
.wbig small{{font-family:Onest;font-size:14px;color:var(--muted);font-weight:600;margin-left:4px;letter-spacing:0}}
.wdelta{{font-weight:800;font-size:15px;color:var(--muted)}}
.wdelta.g{{color:var(--gd)}}.wdelta.b{{color:#B45309}}
/* График веса убран намеренно: на двух-трёх записях он показывал не тренд, а
   шум, и занимал полкарточки. Число и дельта говорят то же самое честнее. */
.wadd{{display:flex;gap:8px;margin-top:12px}}
.wadd input{{flex:1;min-width:0;border:1.5px solid var(--line);border-radius:12px;padding:12px 14px;font-size:16px;background:var(--bg);color:var(--ink)}}
.wadd button{{border:none;border-radius:12px;background:var(--g);color:#fff;font-weight:700;font-size:15px;padding:0 18px;cursor:pointer;white-space:nowrap}}
.whint{{font-size:12px;color:var(--muted);margin-top:9px;line-height:1.4}}
.prefcard{{border-radius:var(--rx);padding:18px}}
.params{{padding:6px 18px}}
/* Итог четырьмя плитками: вес, серия, выполненные дни, вода. Числа крупные —
   на этот экран заходят посмотреть результат, а не читать. */
.pgrid{{display:grid;grid-template-columns:1fr 1fr;gap:10px}}
.pcell{{border-radius:var(--rl);padding:14px 15px}}
.pcell .cap{{display:block;font-size:11px;font-weight:700;letter-spacing:.11em;
  text-transform:uppercase;color:var(--muted)}}
.pcell b{{display:block;font-family:Unbounded;font-weight:700;font-size:26px;letter-spacing:-.045em;
  color:var(--ink);margin-top:8px}}
.pcell b small{{font-family:Onest;font-size:12px;font-weight:600;color:var(--muted);
  letter-spacing:0;margin-left:3px}}
.pcell.good b{{color:var(--gd)}}
.pcell.bad b{{color:#B45309}}
.pcell .psub{{display:block;font-size:12px;color:var(--muted);margin-top:5px}}
/* Окно подписки */
.submodal{{position:fixed;inset:0;z-index:80;display:flex;align-items:center;justify-content:center;
  padding:20px;background:rgba(20,28,18,.5);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px)}}
.submodal[hidden]{{display:none}}
.subwrap{{position:relative;max-width:420px;width:100%;background:var(--card);
  border-radius:var(--rx);padding:8px 18px 18px;box-shadow:0 30px 70px -20px rgba(0,0,0,.5);
  max-height:88vh;overflow:auto}}
.subwrap .sec{{margin-top:14px}}
/* Внутри окна карточка подписки — уже не карточка: окно само задаёт границу,
   а стекло в стекле читается как грязь. */
.subwrap .subcard{{background:none!important;border:0!important;box-shadow:none!important;padding:0}}
.subx{{position:absolute;top:10px;right:10px;width:34px;height:34px;border:0;border-radius:12px;
  background:color-mix(in srgb,var(--ink) 6%,transparent);color:var(--ink-2);font-size:20px;
  line-height:1;cursor:pointer}}
.prow{{display:flex;align-items:center;gap:12px;padding:13px 0;font-size:14px;
  border-bottom:1px solid var(--glass-line)}}
.prow:last-child{{border-bottom:none}}
.prow span{{color:var(--muted);flex:1 1 auto;min-width:0}}
.prow b{{font-weight:600;text-align:right}}
.prow.col{{display:block}}
.prow.col span{{display:block;margin-bottom:9px}}
.chips{{display:flex;flex-wrap:wrap;gap:7px}}
.chips i{{font-style:normal;background:color-mix(in srgb,var(--ink) 5%,transparent);
  border-radius:var(--rs);padding:7px 11px;font-size:12.5px}}
.phint{{font-size:13px;color:var(--muted);line-height:1.5;margin-bottom:12px}}
.prefcard textarea{{width:100%;min-height:60px;resize:vertical;border:1.5px solid var(--line);border-radius:12px;padding:12px 14px;font-size:15px;font-family:inherit;background:var(--bg);color:var(--ink)}}
.prefcard textarea:focus{{outline:none;border-color:var(--g)}}
#savePrefs{{margin-top:12px;width:100%;border:none;border-radius:12px;background:var(--g);color:#fff;font-weight:800;font-size:15px;padding:14px;cursor:pointer}}
#savePrefs:disabled{{opacity:.6}}
.pmsg{{display:none;margin-top:12px;font-size:14px;color:var(--gd);font-weight:700}}.pmsg.s{{display:block}}
.welcome{{position:fixed;inset:0;z-index:50;background:rgba(20,50,31,.55);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:20px}}
.welcome[hidden]{{display:none}}
/* Приветственное окно стоит на затемнении — стекло тут не к месту, нужен
   непрозрачный лист. Селектор длиннее, чем у общего стеклянного правила,
   поэтому перебивает его без гонки !important. */
.welcome .wcard{{background:var(--card)!important;border:1px solid var(--glass-line)!important;
  border-radius:var(--rx);padding:26px 24px 20px;max-width:360px;width:100%;text-align:center;
  box-shadow:0 30px 70px -20px rgba(0,0,0,.5)!important;animation:in .35s ease}}
.wmasc{{width:104px;height:104px;border-radius:26px;overflow:hidden;background:var(--soft);margin:0 auto 18px}}
.wmasc video{{width:100%;height:100%;object-fit:cover;display:block}}
.wstep h3{{font-size:21px;font-weight:800;letter-spacing:-.01em;margin-bottom:8px}}
.wstep p{{font-size:15px;color:var(--muted);line-height:1.5}}
.wdots{{display:flex;gap:7px;justify-content:center;margin:20px 0 16px}}
.wdot{{width:7px;height:7px;border-radius:50%;background:var(--line);transition:all .2s}}
.wdot.on{{background:var(--g);width:20px;border-radius:99px}}
.wnext{{width:100%;border:none;border-radius:14px;background:var(--g);color:#fff;font-weight:800;font-size:16px;padding:15px;cursor:pointer}}
.wskip{{margin-top:10px;border:none;background:none;color:var(--muted);font-size:14px;font-weight:600;cursor:pointer}}
</style></head><body>
<div class="aura" aria-hidden="true"></div>
<header><div class="brand"><span class="dot"></span>NutriPlan<button id="install" class="ins" hidden>Установить</button></div></header>
<div class="welcome" id="welcome" hidden>
  <div class="wcard">
    <div class="wmasc"><video autoplay loop muted playsinline poster="/assets/avocado_wave_sm.png"><source src="/assets/avocado_wave_sm.mp4" type="video/mp4"></video></div>
    <div class="wstep"><h3>Привет! Это твой план</h3><p>Персональное меню на неделю — под твою цель, вкусы и ритм. Я рядом каждый день.</p></div>
    <div class="wstep"><h3>Отмечай, что приготовил</h3><p>Жми «Приготовил» на блюдах — собирай серию дней подряд и держи темп без срывов.</p></div>
    <div class="wstep"><h3>Следи за прогрессом</h3><p>Каждый день отмечай воду и записывай вес — увидишь, как двигаешься к цели.</p></div>
    <div class="wdots"><span class="wdot"></span><span class="wdot"></span><span class="wdot"></span></div>
    <button class="wnext" id="wnext">Далее</button>
    <button class="wskip" id="wskip">Пропустить</button>
  </div>
</div>
<div class="wrap">
  <!-- Экран «Сегодня»: день, а не весь свиток. Раньше страница была 5292 px —
       восемь экранов подряд без всякой навигации, и вся неделя рисовалась разом.
       Разделы те же самые, просто разложены по вкладкам. -->
  <section class="scr on" id="sc-today">
    <h1>{title}</h1><p class="lead">Персонально под твою цель, вкусы и ритм</p>
    {week_over}
    <!-- Крупно то, что человек спрашивает у экрана: сколько ещё можно съесть.
         Норма — это цель, а не ответ на вопрос «сколько осталось». -->
    <div class="norm"><div class="cap">Осталось на день</div>
      <div class="big"><span id="calLeft">{pl.get('cal','')}</span><i>ккал</i></div>
      <div class="sub" id="calSub"></div>
      <div class="track" id="track"></div>
      <!-- «Набрано из нормы», а не одна норма: иначе плитка выглядит как факт
           съеденного и противоречит полосе калорий рядом. Числа проставляет
           paint() по отмеченным приёмам. -->
      <div class="macros">
        <div><div class="mrow"><span class="mic"><img src="/assets/macro-prot.svg" alt="" loading="lazy"></span>
          <span class="mtx"><b><u id="gotP">0</u><i>/{pl.get('P','')}</i></b><span>белки, г</span></span></div>
          <div class="mbar"><i id="barP"></i></div></div>
        <div><div class="mrow"><span class="mic"><img src="/assets/macro-fat.svg" alt="" loading="lazy"></span>
          <span class="mtx"><b><u id="gotF">0</u><i>/{pl.get('F','')}</i></b><span>жиры, г</span></span></div>
          <div class="mbar"><i id="barF"></i></div></div>
        <div><div class="mrow"><span class="mic"><img src="/assets/macro-carb.svg" alt="" loading="lazy"></span>
          <span class="mtx"><b><u id="gotC">0</u><i>/{pl.get('C','')}</i></b><span>углеводы, г</span></span></div>
          <div class="mbar"><i id="barC"></i></div></div>
      </div></div>
    <div class="tiles">
      <!-- Серия — ряд дней плана: закрытые цветные, остальные серые. Одинокое
           число показывало достижение и молчало о том, сколько ещё идти. -->
      <div class="streakc">
        <div class="wtop"><b>Серия</b><span id="prog">0</span></div>
        <div class="flames" id="flames"></div>
        <div class="ssub" id="streakmsg">Отмечай «Приготовил» — собери серию</div>
      </div>
      <div class="water"><div class="wtop"><b>Вода сегодня</b><span id="wnum">0 / {water_goal} ст.</span></div>
        <div class="wcups" id="wcups"></div></div>
    </div>
    <div class="tabs">{tabs}</div>
    {panels}
  </section>

  <!-- Экран «Неделя»: обзор семи дней. Нажатие открывает день ТОЛЬКО ПОСМОТРЕТЬ —
       отмечать и заменять можно на «Сегодня», иначе легко закрыть чужой день. -->
  <section class="scr" id="sc-week">
    <h1>Неделя</h1><p class="lead">Нажми на день, чтобы посмотреть меню</p>
    <div class="days">{week_rows}</div>
  </section>

  <section class="scr" id="sc-cart">
    <h1>Покупки</h1><p class="lead">Отмечай купленное — отметки сохраняются</p>
    {_shopping(pl.get('shopping') or [], pl.get('days') or [])}
  </section>

  <section class="scr" id="sc-me">
    <h1>Я</h1><p class="lead">Прогресс, план и вкусы</p>
  <!-- Сначала итог: где я по весу, держусь ли темпа. Настройки — ниже, их
       открывают раз в неделю, а результат смотрят каждый день. -->
  <section class="sec"><h2>Прогресс</h2>
    <div class="pgrid">
      <div class="pcell"><span class="cap" id="pgwlab">Изменение веса</span>
        <b id="pgw">—</b><span class="psub" id="pgwsub">запиши вес</span></div>
      <div class="pcell"><span class="cap">Серия</span>
        <b id="pgs">0</b><span class="psub">дней подряд</span></div>
      <div class="pcell"><span class="cap">Выполнено</span>
        <b id="pgd">0</b><span class="psub">из {len(days)} дней</span></div>
      <div class="pcell"><span class="cap">Вода</span>
        <b id="pgv">0</b><span class="psub">стаканов сегодня</span></div>
    </div></section>
  {_params(pl, goal_code, water_goal)}
  <section class="sec" id="weightsec"><h2>Твой вес</h2>
    <div class="wcard">
      <div class="wrow"><div class="wbig"><span id="wcur">—</span><small>кг</small></div>
        <div class="wdelta" id="wdelta"></div></div>
      <div class="wadd"><input type="number" inputmode="decimal" step="0.1" id="winput" placeholder="Вес сегодня, кг">
        <button id="wsave">Записать</button></div>
      <div class="whint" id="whint"></div></div></section>
  <section class="sec" id="prefsec"><h2>Что ты не ешь</h2>
    <div class="prefcard">
      <p class="phint">Перечисли продукты через запятую — уберём их из меню и рецептов, и пересоберём план.</p>
      <textarea id="excl" placeholder="напр. грибы, кинза, печень, кофе">{exclude}</textarea>
      <button id="savePrefs">Сохранить и пересобрать план</button>
      <div class="pmsg" id="pmsg">Пересобираю план под твои исключения — это займёт до минуты…</div>
    </div></section>
  {_tips(pl.get('tips') or [])}
  <footer class="plegal">
    <!-- «Войти по почте» отсюда убрана: страницу открывают уже вошедшими, и
         ссылка предлагала сделать то, что уже сделано. На её месте — подписка:
         управляют ею редко, но искать её должно быть очевидно где. -->
    <div class="plinks"><a href="/offer">Оферта</a><a href="/privacy">Политика ПДн</a><a href="/consent">Согласие</a>{subs_link}</div>
    <!-- Оговорка в подвале, мелким шрифтом: то же, что уже есть в оферте и в
         письмах, но теперь и в самом продукте. Мелким — не значит спрятанным:
         текст читаемый и контрастный. Оговорка, которую суд признает скрытой,
         не защищает вовсе, так что «сделать невидимой» работало бы против цели. -->
    <div class="pdisc">План носит рекомендательный, информационно-справочный характер и не является
      медицинской услугой, диагностикой, лечением или назначением лечебной диеты. При заболеваниях,
      беременности и особенностях здоровья проконсультируйтесь с врачом.</div>
    <div class="preq">Самозанятый Ульянин Никита Юрьевич · ИНН 772459697062 · <a href="mailto:support@mynutriplan.ru">support@mynutriplan.ru</a></div>
  </footer>
  </section>
</div>

<!-- Подписка живёт в окне, а не блоком в профиле: управляют ею раз в месяц, а
     место она занимала постоянно — и «Отменить подписку» красной строкой
     маячила там, где человек просто смотрит свой прогресс. -->
<div class="submodal" id="submodal" hidden>
  <div class="subwrap" role="dialog" aria-modal="true" aria-label="Подписка">
    <button class="subx" id="subclose" type="button" aria-label="Закрыть">&times;</button>
    {acct}
  </div>
</div>

<!-- Просмотр дня недели. Отдельный экран, а не всплывашка: меню дня — это пять
     карточек с фото, шторка съела бы половину. -->
<section class="dayview" id="dayview" aria-hidden="true">
  <div class="dvtop"><button id="dvback" aria-label="Назад">{_ic_back()}</button><b id="dvtitle"></b></div>
  <div class="dvbody" id="dvbody"></div>
</section>

<!-- Экран блюда. Открывается тапом по строке приёма: фото, КБЖУ, состав, шаги
     и действия. В списке всего этого нет намеренно — там читают на бегу. -->
<section class="dishv" id="dishview" aria-hidden="true">
  <div class="dvtop"><button id="dishback" aria-label="Назад">{_ic_back()}</button><b id="dishslot"></b></div>
  <div class="dvbody" id="dishbody"></div>
  <div class="dact" id="dishact"></div>
</section>

<nav class="bnav">
  <button class="on" data-s="today">{_ic_home()}<span>Сегодня</span></button>
  <button data-s="week">{_ic_week()}<span>Неделя</span></button>
  <button data-s="cart">{_ic_cart()}<span>Покупки</span></button>
  <button data-s="me">{_ic_me()}<span>Я</span></button>
</nav>
<script>
function activateDay(i){{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('on',+x.dataset.d===i));
  document.querySelectorAll('.panel').forEach(p=>p.classList.toggle('on',+p.dataset.d===i));
}}
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{{
  // БЖУ считается по ОТКРЫТОМУ дню, поэтому смена вкладки его пересчитывает.
  // Пересчёт живёт здесь, а не в activateDay: тот вызывается при загрузке, когда
  // хранилище отметок ещё не объявлено, и paint() падал бы в мёртвой зоне —
  // ровно это и случилось: обработчики ниже переставали навешиваться целиком.
  const i=+t.dataset.d; activateDay(i); paint(); markToday(); history.replaceState(null,'','#d'+i);
  window.scrollTo({{top:0,behavior:'smooth'}});
}}));
const NDAYS={len(days)};
// В списке недели помечаем строку открытого дня: без пометки семь одинаковых
// строк не говорят, где ты сейчас.
function markToday(){{
  const cur=document.querySelector('#sc-today .panel.on');
  document.querySelectorAll('.drow .dnow').forEach(e=>{{
    e.hidden = !cur || e.closest('.drow').dataset.d !== cur.dataset.d;
  }});
}}
// Кнопкам «Сегодня»/«Завтра» день проставляем здесь: сегодняшний день плана
// знает только клиент. «Завтра» показываем, только если оно в плане есть —
// иначе кнопка вела бы в пустоту в последний день недели.
const TODAY=(function(){{let wd=(new Date().getDay()+6)%7; return wd<NDAYS?wd:0;}})();
document.querySelectorAll('.tab[data-rel]').forEach(t=>{{
  const d=TODAY+(+t.dataset.rel);
  if(d>=NDAYS){{t.remove();return;}}
  t.dataset.d=d; t.hidden=false;
}});
(function(){{const m=location.hash.match(/d(\\d+)/);
  activateDay(m?parseInt(m[1]):TODAY); markToday();}})();
// app-loop: отметки «приготовил» + прогресс (localStorage)
// Версия плана — в ключе отметок: иначе новая неделя открывается с галочками
// старой, а серверный сброс прогресса тут же перетирается локальным состоянием.
const T='{token}', VER='{ver}', DKEY='np_done_'+T+(VER?'_v'+VER:'');
const START_W={start_w or 0}, GOAL="{goal_code}", WGOAL={water_goal};
// онбординг-тур (показываем один раз на план)
(function(){{
  const ov=document.getElementById('welcome'); if(!ov) return;
  const KEY='np_welcome_'+T; if(localStorage.getItem(KEY)) return;
  const steps=ov.querySelectorAll('.wstep'), dots=ov.querySelectorAll('.wdot'), btn=document.getElementById('wnext');
  let step=0;
  function show(i){{steps.forEach((s,j)=>s.style.display=j===i?'block':'none');dots.forEach((d,j)=>d.classList.toggle('on',j===i));btn.textContent=i===steps.length-1?'Погнали!':'Далее';}}
  function fin(){{localStorage.setItem(KEY,'1');ov.setAttribute('hidden','');}}
  btn.onclick=()=>{{step<steps.length-1?show(++step):fin();}};
  document.getElementById('wskip').onclick=fin;
  show(0); ov.removeAttribute('hidden');
}})();
// Перенос отметок со старого ключа «день:слот» на новый «день:номер:слот».
// Если слот в дне один — отметка переезжает; если слотов с таким названием два,
// понять, какой из них отмечали, невозможно, поэтому такую отметку снимаем
// (лучше снять галочку, чем поставить две чужих).
function migrateDone(o){{
  let ch=false;
  Object.keys(o||{{}}).forEach(k=>{{
    const p=k.split(':'); if(p.length!==2) return;
    const els=[...document.querySelectorAll("#sc-today .panel[data-d='"+p[0]+"'] .meal")]
      .filter(el=>(el.dataset.k||'').split(':').slice(2).join(':')===p[1]);
    delete o[k]; ch=true;
    if(els.length===1) o[els[0].dataset.k]=1;
  }});
  return ch;
}}
let done=JSON.parse(localStorage.getItem(DKEY)||'{{}}');
if(migrateDone(done)) localStorage.setItem(DKEY,JSON.stringify(done));
function dayComplete(i){{const ks=[...document.querySelectorAll("#sc-today .panel[data-d='"+i+"'] .meal")].map(el=>el.dataset.k);return ks.length>0 && ks.every(k=>done[k]);}}
function paint(){{
  document.querySelectorAll('.meal').forEach(el=>el.classList.toggle('on', !!done[el.dataset.k]));
  // Серия считается по ВСЕЙ неделе, а не по числу кнопок наверху: кнопок теперь
  // две («Сегодня»/«Завтра»), и привязка к ним превратила бы серию в «0 / 2».
  let comp=0, run=0, best=0;
  for(let i=0;i<NDAYS;i++){{const c=dayComplete(i);
    if(c){{comp++;run++;best=Math.max(best,run);}} else run=0;}}
  document.querySelectorAll('.tab[data-d]').forEach(t=>
    t.classList.toggle('complete',dayComplete(+t.dataset.d)));
  const pr=document.getElementById('prog'); if(pr) pr.textContent=comp+' / '+NDAYS;
  // Ряд огоньков: закрытые дни цветные. Рисуем по ТЕМ ЖЕ отметкам, что и число,
  // иначе плитка начнёт противоречить сама себе.
  const fl=document.getElementById('flames');
  if(fl) fl.innerHTML=Array.from({{length:NDAYS}},(_,i)=>
    '<span class="'+(dayComplete(i)?'':'off')+'"><img src="/assets/streak-flame.svg" alt="" loading="lazy"></span>').join('');
  const wo=document.getElementById('woDone'); if(wo) wo.textContent=comp;   // итог недели
  const sm=document.getElementById('streakmsg');
  if(sm) sm.innerHTML = best>=2 ? ('Серия <b>'+best+'</b> дней подряд — так держать!')
    : comp>0 ? 'Отличное начало! Не бросай серию' : 'Отмечай «Приготовил» — собери серию';
  // Карточка наверху — про ОТКРЫТЫЙ день: сколько ещё можно съесть, сколько уже
  // съедено и какими приёмами. Шкала сегментами по приёмам, а не процентами.
  const panel=document.querySelector('#sc-today .panel.on');
  if(panel){{
    const tot=+panel.dataset.tot||0;
    const meals=[...panel.querySelectorAll('.meal')];
    let eaten=0; meals.forEach(el=>{{ if(done[el.dataset.k]) eaten+=(+el.dataset.kc||0); }});
    const left=tot-eaten, over=left<0;
    const cl=document.getElementById('calLeft');
    if(cl) cl.textContent=over?('+'+Math.abs(left)):left;
    const cp=document.querySelector('.norm .cap');
    if(cp) cp.textContent=over?'Перебор за день':'Осталось на день';
    const cs=document.getElementById('calSub');
    if(cs) cs.textContent='Съедено '+eaten+' из '+tot+' ккал';
    const tr=document.getElementById('track');
    if(tr) tr.innerHTML=meals.map(el=>
      '<i class="'+(done[el.dataset.k]?(over?'over':'on'):'')+'"></i>').join('');
  }}
  // БЖУ: набрано из нормы по ОТКРЫТОМУ дню — по тем же отметкам, что и калории.
  ['P','F','C'].forEach(m=>{{
    const num=document.getElementById('got'+m), bar=document.getElementById('bar'+m);
    if(!num||!bar) return;
    let got=0;
    document.querySelectorAll('#sc-today .panel.on .meal').forEach(el=>{{
      if(done[el.dataset.k]) got+=(+el.dataset[m.toLowerCase()]||0);
    }});
    const goal=parseInt(num.parentNode.querySelector('i').textContent.replace(/\\D/g,''),10)||0;
    num.textContent=got;
    bar.style.width=(goal?Math.min(100,Math.round(got/goal*100)):0)+'%';
  }});
  // Кнопка «Приготовил» на экране блюда — копия, вынутая из строки. Красим её по
  // ключу, а не по родителю: у копии родителя-.meal нет.
  document.querySelectorAll('.done').forEach(b=>b.classList.toggle('on', !!done[b.dataset.k]));
  // Плитки прогресса в профиле — из тех же чисел, что и всё остальное на экране.
  const pgs=document.getElementById('pgs'); if(pgs) pgs.textContent=best;
  const pgd=document.getElementById('pgd'); if(pgd) pgd.textContent=comp;
}}
// Отметка «приготовил» приходит из двух мест сразу: галочка в строке и кнопка на
// экране блюда (а она ещё и клон). Поэтому делегирование, а не привязка к
// конкретным узлам — иначе клон был бы мёртвой кнопкой.
document.addEventListener('click',e=>{{
  const b=e.target.closest('.done,.tick'); if(!b) return;
  const k=b.dataset.k; if(!k) return;
  if(done[k])delete done[k]; else done[k]=1;
  localStorage.setItem(DKEY,JSON.stringify(done)); paint(); pushProgress();
}});
paint();
// трекер воды (сброс по дню)
const WK=()=>'np_water_'+T+'_'+new Date().toISOString().slice(0,10);
// Стакан силуэтом: сужается книзу и скруглён по дну. Прежний прямоугольник со
// скруглением читался как индикатор заряда, а не как стакан воды.
const CUP="<svg viewBox='0 0 20 26' fill='currentColor'><path d='M3.4 1h13.2a1.3 1.3 0 0 1 1.29 1.44l-1.72 20.3A3.2 3.2 0 0 1 12.98 25.6H7.02a3.2 3.2 0 0 1-3.19-2.86L2.11 2.44A1.3 1.3 0 0 1 3.4 1Z'/></svg>";
function renderWater(){{
  const c=document.getElementById('wcups'); if(!c)return;
  const n=parseInt(localStorage.getItem(WK())||'0'); c.innerHTML='';
  for(let i=1;i<=WGOAL;i++){{const b=document.createElement('button');b.className='cup'+(i<=n?' f':'');b.innerHTML=CUP;
    b.onclick=()=>{{let cur=parseInt(localStorage.getItem(WK())||'0');cur=(cur===i)?i-1:i;localStorage.setItem(WK(),cur);renderWater();pushProgress();}};
    c.appendChild(b);}}
  const nn=document.getElementById('wnum'); if(nn)nn.textContent=n+' / '+WGOAL+' ст.';
  const pv=document.getElementById('pgv'); if(pv)pv.innerHTML=n+'<small>/'+WGOAL+'</small>';
}}
renderWater();
// вес + тренд
const WTK='np_wt_'+T, MN=['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
const fmtd=s=>{{const d=new Date(s);return d.getDate()+' '+MN[d.getMonth()];}};
const loadWt=()=>{{try{{return JSON.parse(localStorage.getItem(WTK)||'[]');}}catch(e){{return[];}}}};
function renderWeight(){{
  const a=loadWt(),base=START_W>0?START_W:(a[0]?a[0].w:0),cur=a.length?a[a.length-1].w:(START_W>0?START_W:0);
  const ce=document.getElementById('wcur');if(ce)ce.textContent=cur?String(cur).replace('.',','):'—';
  const de=document.getElementById('wdelta');
  if(de){{if(cur&&base){{const diff=Math.round((cur-base)*10)/10;
    if(Math.abs(diff)<0.05){{de.textContent='±0 кг';de.className='wdelta';}}
    else{{const good=GOAL==='gain'?diff>0:GOAL==='lose'?diff<0:true;
      de.textContent=(diff>0?'+':'')+String(diff).replace('.',',')+' кг';de.className='wdelta '+(good?'g':'b');}}
  }}else de.textContent='';}}
  const h=document.getElementById('whint');
  if(h)h.textContent=a.length?('Записей: '+a.length+' · последняя '+fmtd(a[a.length-1].d)):
    (START_W>0?('Старт из анкеты — '+String(START_W).replace('.',',')+' кг. Записывай раз в неделю — увидишь тренд.'):'Записывай вес раз в неделю — увидишь тренд.');
  // Плитка веса в «Прогрессе»: то же число, что и в карточке ниже, но с ответом
  // на вопрос «сколько всего» — от старта из анкеты, а не от прошлой записи.
  const pw=document.getElementById('pgw'), ps=document.getElementById('pgwsub'),
        pl=document.getElementById('pgwlab'), cell=pw&&pw.closest('.pcell');
  if(pw&&cur&&base){{
    const diff=Math.round((cur-base)*10)/10;
    pw.innerHTML=(diff>0?'+':diff<0?'−':'±')+String(Math.abs(diff)).replace('.',',')+'<small>кг</small>';
    if(pl) pl.textContent=a.length>1?'Изменение веса':'Старт';
    if(ps) ps.textContent=String(base).replace('.',',')+' → '+String(cur).replace('.',',')+' кг';
    const good=GOAL==='gain'?diff>0:GOAL==='lose'?diff<0:true;
    if(cell) cell.className='pcell '+(Math.abs(diff)<0.05?'':(good?'good':'bad'));
  }}else if(pw){{ pw.textContent='—'; if(ps) ps.textContent='запиши вес'; }}
}}
const wsv=document.getElementById('wsave');
if(wsv)wsv.onclick=()=>{{const el=document.getElementById('winput');const v=parseFloat((el.value||'').replace(',','.'));
  if(!v||v<30||v>350){{el.style.borderColor='#DC2626';return;}}el.style.borderColor='';
  const a=loadWt(),today=new Date().toISOString().slice(0,10),i=a.findIndex(e=>e.d===today);
  if(i>=0)a[i].w=v;else a.push({{d:today,w:v}});a.sort((x,y)=>x.d<y.d?-1:1);
  localStorage.setItem(WTK,JSON.stringify(a));el.value='';renderWeight();pushProgress();}};
renderWeight();
// ---- серверная синхронизация прогресса (стрик/вода/вес) к аккаунту ----
let _srvread=false;   // прочитали ли мы серверное состояние хоть раз (см. t в collectLocal)
function collectLocal(){{
  const water={{}}; const wp='np_water_'+T+'_';
  for(let i=0;i<localStorage.length;i++){{const k=localStorage.key(i);
    if(k&&k.indexOf(wp)===0){{const v=parseInt(localStorage.getItem(k)||'0'); if(v)water[k.slice(wp.length)]=v;}}}}
  let weight=[]; try{{weight=JSON.parse(localStorage.getItem('np_wt_'+T)||'[]');}}catch(e){{}}
  // t = версия для merge на сервере. Если серверное состояние прочитать не удалось,
  // шлём t=0: сервер тогда доливает, а не заменяет. Иначе клиент с упавшим GET
  // отправляет пустой done и стирает прогресс — после смены ver локально пусто.
  return {{done:done, water:water, weight:weight, t:_srvread?Date.now():0}};
}}
function applyLocal(p){{
  if(!p)return;
  done=p.done||{{}}; localStorage.setItem(DKEY,JSON.stringify(done));
  Object.entries(p.water||{{}}).forEach(([d,n])=>localStorage.setItem('np_water_'+T+'_'+d,n));
  localStorage.setItem('np_wt_'+T,JSON.stringify(p.weight||[]));
}}
let _pushT=null, _synced=false;
function pushProgress(){{if(!_synced)return;clearTimeout(_pushT);_pushT=setTimeout(()=>{{
  fetch('/api/plan/'+T+'/progress',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(collectLocal())}}).catch(()=>{{}});
}},1000);}}
// начальная синхронизация ОДИН раз: сервер ∪ локальное → перерисовать → отправить объединённое (дальше replace)
fetch('/api/plan/'+T+'/progress').then(r=>r.json()).then(srv=>{{
  _srvread=true;
  const loc=collectLocal();
  const mDone=Object.assign({{}},srv.done||{{}},loc.done||{{}}); migrateDone(mDone);  // на сервере тоже лежат старые ключи
  const mWater=Object.assign({{}},srv.water||{{}}); Object.entries(loc.water||{{}}).forEach(([d,n])=>{{mWater[d]=Math.max(mWater[d]||0,n);}});
  const wt={{}}; (srv.weight||[]).concat(loc.weight||[]).forEach(e=>{{if(e&&e.d)wt[e.d]=e;}});
  applyLocal({{done:mDone,water:mWater,weight:Object.values(wt).sort((a,b)=>a.d<b.d?-1:1)}});
  paint();renderWater();renderWeight();_synced=true;pushProgress();
}}).catch(()=>{{_synced=true;}});
// подгрузка фото блюд по мере генерации (первый юзер видит их через ~10–20с)
(function(){{let tries=0;function poll(){{tries++;
  // Селектор именно '.mimg img': сама марка теперь <button>, а картинка внутри.
  const imgs=[...document.querySelectorAll('.mimg img[data-slug]')].filter(im=>!im.dataset.ready);
  if(!imgs.length)return;
  imgs.forEach(im=>{{fetch(im.getAttribute('src'),{{method:'HEAD'}}).then(r=>{{
    if((r.headers.get('content-type')||'').indexOf('webp')>=0){{im.dataset.ready='1';im.src='/dish/'+im.dataset.slug+'?v='+Date.now();}}
  }}).catch(()=>{{}});}});
  if(tries<6)setTimeout(poll,5000);
}}setTimeout(poll,5000);}})();
// замена блюда (LLM-регенерация одного блюда, затем перезагрузка на том же дне)
document.addEventListener('click',async e=>{{
  const b=e.target.closest('.swap'); if(!b) return;
  const day=+b.dataset.day, slot=b.dataset.slot, idx=+b.dataset.i, o=b.textContent; b.disabled=true; b.textContent='Подбираю…';
  try{{
    // idx — номер приёма в дне. Слоты повторяются («Перекус» ×2), и по одному
    // slot сервер не отличит второй перекус от первого. Лишнее поле сервер
    // просто игнорирует, пока не начнёт его использовать.
    const r=await fetch('/api/plan/{token}/swap',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{day,slot,idx}})}});
    const j=await r.json(); if(!j.meal) throw 0;
    delete done[b.dataset.k]; localStorage.setItem(DKEY,JSON.stringify(done));  // новое блюдо — сбрасываем «съедено»
    try{{await fetch('/api/plan/'+T+'/progress',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(collectLocal())}});}}catch(e){{}}
    location.hash='d'+day; location.reload();
  }}catch(e){{ b.disabled=false; b.textContent=o; alert('Не удалось заменить — попробуй ещё раз'); }}
}});
// отвязка карты (без отмены подписки) — двойное подтверждение
const ub=document.getElementById('unbindCard');
if(ub){{let a2=false;ub.addEventListener('click',async()=>{{
  if(!a2){{a2=true;ub.textContent='Нажми ещё раз, чтобы отвязать карту';return;}}
  ub.disabled=true;ub.textContent='Отвязываю…';
  try{{
    const r=await fetch('/api/sub/'+ub.dataset.token+'/unbind-card',{{method:'POST'}});
    const j=await r.json();if(!j.ok)throw 0;
    const m=document.getElementById('cmsg');
    m.textContent='Карта отвязана — автосписаний больше не будет. Подписка активна до конца оплаченного периода.';
    m.classList.add('s');ub.style.display='none';
    const note=document.querySelector('.subcard .cnote');if(note)note.textContent='Карта не привязана — автосписаний не будет';
  }}catch(e){{ub.disabled=false;ub.textContent='Отвязать карту';alert('Не удалось отвязать карту. Напиши на support@mynutriplan.ru');}}
}});}}
// отмена подписки — двойное подтверждение
const cb=document.getElementById('cancelSub');
if(cb){{let armed=false;cb.addEventListener('click',async()=>{{
  if(!armed){{armed=true;cb.textContent='Нажми ещё раз, чтобы подтвердить';return;}}
  cb.disabled=true;cb.textContent='Отменяю…';
  try{{
    const r=await fetch('/api/sub/'+cb.dataset.token+'/cancel',{{method:'POST'}});
    const j=await r.json();if(!j.ok)throw 0;
    const m=document.getElementById('cmsg');
    m.textContent='Подписка отменена, карта отвязана. Автосписаний больше не будет — доступ сохраняется до конца оплаченного периода.';
    m.classList.add('s');cb.style.display='none';
    if(ub)ub.style.display='none';
    const a=document.querySelector('.sactive');if(a)a.outerHTML="<div class='scanceled'>Отменена</div>";
  }}catch(e){{cb.disabled=false;cb.textContent='Отменить подписку';alert('Не удалось отменить. Напиши на support@mynutriplan.ru');}}
}});}}
// «Что ты не ешь» — сохранить исключения и пересобрать план
const sp=document.getElementById('savePrefs');
if(sp)sp.onclick=async()=>{{
  const v=(document.getElementById('excl').value||'').trim();
  sp.disabled=true;sp.textContent='Пересобираю…';const pm=document.getElementById('pmsg');if(pm)pm.classList.add('s');
  try{{
    const r=await fetch('/api/plan/'+T+'/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{exclude:v}})}});
    const j=await r.json();if(!j.ok)throw 0;
    location.reload();
  }}catch(e){{sp.disabled=false;sp.textContent='Сохранить и пересобрать план';if(pm)pm.classList.remove('s');alert('Не удалось пересобрать. Попробуй ещё раз или напиши support@mynutriplan.ru');}}
}};
// «Не нравится» — добавить блюдо в стоп-лист и пересобрать план (тяжёлая LLM-операция → подтверждение)
document.addEventListener('click',async e=>{{
  const b=e.target.closest('.dislike'); if(!b) return;
  // Подсказку рисуем строкой в самой строке действий: title на телефоне не
  // виден, а без обратной связи первый тап выглядит как «ничего не произошло».
  let hint=b.parentNode.querySelector('.hintx');
  if(!hint){{hint=document.createElement('div');hint.className='hintx';
    hint.textContent='Нажми ещё раз — уберу это блюдо и пересоберу план';
    b.parentNode.appendChild(hint);}}
  if(!b.dataset.armed){{b.dataset.armed='1';b.classList.add('armed');hint.classList.add('show');
    b.title='Нажми ещё раз — уберу это блюдо и пересоберу план';
    setTimeout(()=>{{delete b.dataset.armed;b.classList.remove('armed');hint.classList.remove('show');
      b.title='Не нравится — убрать из меню';}},4000);return;}}
  const name=b.dataset.name||''; document.querySelectorAll('.dislike').forEach(x=>x.disabled=true);
  try{{
    const r=await fetch('/api/plan/'+T+'/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{dislike:name}})}});
    const j=await r.json();if(!j.ok)throw 0; location.hash=''; location.reload();
  }}catch(e){{document.querySelectorAll('.dislike').forEach(x=>x.disabled=false);alert('Не удалось обновить меню — попробуй ещё раз');}}
}});
// Фото блюда на весь экран. В списке марка 56–64px, а в кэше 512px — блюдо в
// такой марке не разглядеть, хотя «понятно, что покупаешь» и есть весь смысл фото.
(function(){{
  const lb=document.createElement('div'); lb.className='lb'; lb.setAttribute('hidden','');
  lb.innerHTML="<button class='x' type='button' aria-label='Закрыть'>&times;</button>"
    +"<figure><img alt=''><figcaption></figcaption></figure>";
  document.body.appendChild(lb);
  const im=lb.querySelector('img'), cap=lb.querySelector('figcaption');
  let opener=null;
  function open(slug,name,btn){{
    opener=btn;
    im.src='/dish/'+encodeURIComponent(slug)+'?lg=1'; im.alt=name||''; cap.textContent=name||'';
    lb.removeAttribute('hidden'); lb.classList.add('on');
    document.body.style.overflow='hidden';       // фон не должен ехать под открытым фото
    lb.querySelector('.x').focus();
  }}
  function close(){{
    lb.classList.remove('on'); lb.setAttribute('hidden','');
    // Фото открывается ПОВЕРХ экрана блюда, и тот тоже держит фон. Снимать
    // блокировку безусловно нельзя: закрыв фото, человек оставался бы на экране
    // блюда, под которым едет страница.
    document.body.style.overflow=document.querySelector('.dishv.on, .dayview.on')?'hidden':'';
    if(opener){{opener.focus();opener=null;}}     // возвращаем фокус туда, откуда открыли
  }}
  document.addEventListener('click',e=>{{
    // Открывает только большое фото НА ЭКРАНЕ БЛЮДА. В списке фото — часть
    // строки: тап по строке ведёт на блюдо, и два разных исхода у одного
    // жеста были бы лотереей.
    const b=e.target.closest('.dshot[data-zoom]');
    if(b){{open(b.dataset.zoom,b.dataset.name,b);return;}}
    // Клик по фону и по кресту закрывают; по самой картинке — нет.
    if(lb.classList.contains('on') && !e.target.closest('figure')) close();
  }});
  // preventDefault — сигнал экранам под фото, что Escape уже израсходован. Без
  // него один Escape закрывал И фото, И экран блюда: человек хотел вернуться к
  // рецепту, а его выбрасывало в список.
  document.addEventListener('keydown',e=>{{
    if(e.key==='Escape'&&lb.classList.contains('on')){{close();e.preventDefault();}}}});
}})();

// Список покупок: отметки купленного. Раньше это была стена текста без единого
// элемента управления — то есть с ним нельзя было делать ровно то, ради чего
// список и нужен. Отметки держим локально: они про поход в магазин, а не про
// данные аккаунта, и синхронизировать их между устройствами незачем.
(function(){{
  const box=document.getElementById('shop'); if(!box) return;
  // Версия плана — в ключе, как у отметок «приготовил»: крон раз в неделю
  // пересобирает меню на тот же токен, и старые «куплено» на новом списке —
  // это чужие галочки, с которыми человек уходит в магазин. Плюс сам ключ
  // позиции теперь = продукт (см. _shopping_html): версия защищает от новой
  // недели, продукт — от перестановки категорий внутри одной.
  const SUF=T+(VER?'_v'+VER:''), KEY='np_shop_'+SUF, PKEY='np_shopp_'+SUF;
  const out=document.getElementById('sdone');
  // Подчищаем отметки прошлых версий этого же плана — иначе localStorage растёт
  // на один мусорный ключ каждую неделю и когда-нибудь упрётся в квоту.
  try{{
    const mine=['np_shop_'+T,'np_shopp_'+T];   // старый безверсионный вид + все '_vX'
    for(let i=localStorage.length-1;i>=0;i--){{
      const k=localStorage.key(i);
      if(!k||k===KEY||k===PKEY) continue;
      if(mine.some(p=>k===p||k.indexOf(p+'_v')===0)) localStorage.removeItem(k);
    }}
  }}catch(e){{}}
  const prog=document.querySelector('.sprog'), tot=document.getElementById('stot');
  let st={{}}; try{{st=JSON.parse(localStorage.getItem(KEY)||'{{}}');}}catch(e){{}}
  const boxes=[...box.querySelectorAll('input[data-si]')];
  const labels=[...box.querySelectorAll('.si')];
  let shown=labels.length;
  function paint(){{
    const vis=boxes.filter(b=>!b.closest('.si').hidden);
    const n=vis.filter(b=>b.checked).length;
    if(out) out.textContent=n;
    if(tot) tot.textContent=vis.length;
    if(prog) prog.classList.toggle('all', vis.length>0 && n===vis.length);
  }}
  // Период закупки. Дни считаем от СЕГОДНЯШНЕГО дня плана, а не от первого:
  // «сегодня» на пятый день недели — это пятый день, а не понедельник.
  function apply(p){{
    let from=TODAY, to=TODAY;
    if(p==='t'){{from=TODAY+1;to=TODAY+1;}}
    else if(p==='3'){{to=TODAY+2;}}
    else if(p==='7'){{from=0;to=NDAYS-1;}}
    shown=0;
    labels.forEach(l=>{{
      let per={{}}; try{{per=JSON.parse(l.dataset.q||'{{}}');}}catch(e){{}}
      let sum=0, any=false;
      for(let d=from;d<=to;d++){{ if(per[d]!==undefined){{ any=true; sum+=per[d]; }} }}
      l.hidden=!any; if(any) shown++;
      if(any){{
        // Количество пересчитано под период. Ноль — значит в исходной строке
        // количества не было («соль, перец по вкусу»): печатаем как есть.
        const u=l.dataset.u||'', t=l.dataset.t||'';
        const q=sum?(' '+(Math.abs(sum-Math.round(sum))<1e-6?Math.round(sum):sum.toFixed(1).replace('.',','))+(u?' '+u:'')):'';
        l.querySelector('.st').textContent=l.dataset.n+q+(t?' '+t:'');
      }}
    }});
    // Пустую категорию прячем целиком, иначе остаётся заголовок над пустотой.
    box.querySelectorAll('.cat').forEach(c=>{{
      const vis=[...c.querySelectorAll('.si')].filter(x=>!x.hidden);
      c.hidden=vis.length===0;
      const cnt=c.querySelector('.ct span'); if(cnt) cnt.textContent=vis.length;
    }});
    let none=box.querySelector('.snone');
    if(!shown){{
      if(!none){{none=document.createElement('div');none.className='snone';box.appendChild(none);}}
      none.textContent='На этот период покупать нечего.';
      none.hidden=false;
    }} else if(none) none.hidden=true;
    document.querySelectorAll('#shopseg .pseg').forEach(b=>b.classList.toggle('on',b.dataset.p===p));
    try{{localStorage.setItem(PKEY,p);}}catch(e){{}}
    paint();
  }}
  document.querySelectorAll('#shopseg .pseg').forEach(b=>
    b.addEventListener('click',()=>apply(b.dataset.p)));
  boxes.forEach(b=>{{
    b.checked=!!st[b.dataset.si];
    b.addEventListener('change',()=>{{
      if(b.checked) st[b.dataset.si]=1; else delete st[b.dataset.si];
      try{{localStorage.setItem(KEY,JSON.stringify(st));}}catch(e){{}}
      paint();
    }});
  }});
  const clr=document.getElementById('sclear');
  if(clr) clr.addEventListener('click',()=>{{
    st={{}}; try{{localStorage.removeItem(KEY);}}catch(e){{}}
    boxes.forEach(b=>b.checked=false); paint();
  }});
  // Переключателя может не быть: у плана без ингредиентов список недельный и
  // цельный. Тогда никакой фильтрации — иначе apply() спрятал бы ВЕСЬ список,
  // не найдя у позиций разбивки по дням.
  if(document.getElementById('shopseg')){{
    let p0='7'; try{{p0=localStorage.getItem(PKEY)||'7';}}catch(e){{}}
    // «Завтра» в последний день недели показывать нечего — откатываемся на неделю.
    if(p0==='t'&&TODAY+1>=NDAYS) p0='7';
    apply(p0);
  }} else paint();
}})();

// ── вкладки ──────────────────────────────────────────────────────────────
// Разделы те же, что были в свитке; переключаем видимость контейнеров.
document.querySelectorAll('.bnav button').forEach(b=>b.addEventListener('click',()=>{{
  document.querySelectorAll('.bnav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.scr').forEach(s=>s.classList.toggle('on',s.id==='sc-'+b.dataset.s));
  window.scrollTo(0,0);
}}));

// ── просмотр дня недели ───────────────────────────────────────────────────
// Карточки берём из уже отрисованных панелей. Отметку «приготовил» из копии
// убираем — закрывать чужой день человек не собирался, — а замена блюда, замена
// всего дня и подробности блюда остаются: меню недели правят как раз заранее.
(function(){{
  const view=document.getElementById('dayview'), body=document.getElementById('dvbody');
  if(!view) return;
  function open(i){{
    const panel=document.querySelector('#sc-today .panel[data-d="'+i+'"]');
    if(!panel) return;
    const clone=panel.cloneNode(true);
    // Галочка — единственное, чего в чужом дне быть не должно: «съедено» ставят
    // в тот день, когда едят.
    clone.querySelectorAll('.tick').forEach(n=>n.remove());
    clone.querySelectorAll('[id]').forEach(n=>n.removeAttribute('id'));   // без дублей id
    const title=(clone.querySelector('.dtitle')||{{}}).textContent||('День '+(i+1));
    const t=clone.querySelector('.dtitle'); if(t) t.remove();
    document.getElementById('dvtitle').textContent=title.trim().replace(/\\s+(\\d+\\s*ккал)$/,' · $1');
    body.innerHTML='<button class="swapday" data-day="'+i+'">Заменить весь день</button>'
      +'<div class="dvnote">Тап по блюду — состав и рецепт. Отметить «приготовил» можно в тот день, когда готовишь.</div>';
    body.appendChild(clone);
    clone.classList.add('on');
    view.classList.add('on'); view.setAttribute('aria-hidden','false');
    view.scrollTop=0; document.body.style.overflow='hidden';
    history.pushState({{dayview:1}},'');    // системное «назад» закрывает просмотр
  }}
  // Замена целого дня — одна LLM-генерация и заметное ожидание, поэтому кнопка
  // сразу говорит, что происходит, и блокирует себя от второго нажатия.
  document.addEventListener('click',async e=>{{
    const b=e.target.closest('.swapday'); if(!b) return;
    const day=+b.dataset.day, o=b.textContent;
    b.disabled=true; b.textContent='Собираю новый день…';
    let r=null;
    try{{
      r=await fetch('/api/plan/'+T+'/swap-day',{{method:'POST',
        headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{day}})}});
      const j=await r.json(); if(!j.meals) throw 0;
      // Отметки этого дня сняты вместе с блюдами: они были про другую еду.
      Object.keys(done).forEach(k=>{{ if(k.split(':')[0]===String(day)) delete done[k]; }});
      localStorage.setItem(DKEY,JSON.stringify(done));
      try{{await fetch('/api/plan/'+T+'/progress',{{method:'POST',
        headers:{{'Content-Type':'application/json'}},body:JSON.stringify(collectLocal())}});}}catch(e){{}}
      location.hash='v'+day; location.reload();   // v = вернуться в просмотр дня
    }}catch(e){{ b.disabled=false; b.textContent=o;
      alert(r&&r.status===429?'Слишком много замен за час — попробуй позже':'Не удалось собрать новый день — попробуй ещё раз'); }}
  }});
  function close(back){{
    view.classList.remove('on'); view.setAttribute('aria-hidden','true');
    document.body.style.overflow='';
    if(back && history.state && history.state.dayview) history.back();
  }}
  document.querySelectorAll('.drow').forEach(r=>r.addEventListener('click',()=>open(+r.dataset.d)));
  document.getElementById('dvback').addEventListener('click',()=>close(true));
  addEventListener('popstate',()=>{{
    // Над просмотром дня может лежать экран блюда. «Назад» снимает ОДИН слой:
    // этот обработчик зарегистрирован раньше, поэтому просто уступает.
    if(document.getElementById('dishview').classList.contains('on')) return;
    if(view.classList.contains('on')) close(false);
  }});
  // После замены дня страница перезагружается — открываем тот же день снова,
  // иначе человек оказывался на «Сегодня» и не видел результата нажатия.
  (function(){{const m=location.hash.match(/^#v(\\d+)/);
    if(m){{document.querySelector('.bnav button[data-s="week"]').click(); open(+m[1]);}}}})();
  addEventListener('keydown',e=>{{ if(e.key==='Escape'&&view.classList.contains('on')) close(true); }});
}})();

// ── экран блюда ───────────────────────────────────────────────────────────
// Содержимое берём из самой строки: там уже лежат КБЖУ, рецепт и кнопки,
// скрытые в .mhide. Один источник правды — не надо ходить за рецептом на
// сервер и следить, чтобы две копии не разошлись.
(function(){{
  const view=document.getElementById('dishview'), body=document.getElementById('dishbody'),
        act=document.getElementById('dishact'), slotb=document.getElementById('dishslot');
  if(!view) return;
  function open(meal){{
    const h=meal.querySelector('.mhide'); if(!h) return;
    const name=h.dataset.name||'', slug=h.dataset.slug||'', slot=h.dataset.slot||'';
    const day=(meal.closest('.panel')||{{}}).dataset;
    slotb.textContent=slot;
    const kcal=h.dataset.kcal||'', p=meal.dataset.p||'0', f=meal.dataset.f||'0', c=meal.dataset.c||'0';
    const rec=h.querySelector('details');
    let blocks='';
    if(rec){{
      const ing=rec.querySelector('.ing'), st=rec.querySelector('.steps');
      if(ing) blocks+='<div class="dh">Что нужно</div><ul class="ing">'+ing.innerHTML+'</ul>';
      if(st)  blocks+='<div class="dh">Как готовить</div><ol class="steps">'+st.innerHTML+'</ol>';
    }}
    body.innerHTML=
      '<button class="dshot" data-zoom="'+slug+'" data-name="'+name.replace(/"/g,'&quot;')+'" '
        +'aria-label="Открыть фото">'
        +'<img alt="" src="/dish/'+encodeURIComponent(slug)+'?t='+encodeURIComponent(name)+'"></button>'
      +'<h2>'+name+'</h2>'
      +'<div class="dmeta">'+(day&&day.d!==undefined?('День '+(+day.d+1)+' · '):'')+slot.toLowerCase()+'</div>'
      +'<div class="dkcal">'
        +'<div><b>'+kcal+'</b><span>ккал</span></div>'
        +'<div><b>'+p+'<i>г</i></b><span>белки</span></div>'
        +'<div><b>'+f+'<i>г</i></b><span>жиры</span></div>'
        +'<div><b>'+c+'<i>г</i></b><span>углеводы</span></div>'
      +'</div>'+blocks;
    // Кнопки — КОПИЯ из строки: обработчики делегированные, поэтому копия
    // работает так же, как оригинал, и ключ отметки у неё тот же.
    act.innerHTML=''; act.appendChild(h.querySelector('.mact').cloneNode(true));
    paint();
    // Из просмотра чужого дня «Приготовил» не показываем (см. CSS .fromday).
    view.classList.toggle('fromday', !!meal.closest('#dayview'));
    view.classList.add('on'); view.setAttribute('aria-hidden','false');
    view.scrollTop=0; document.body.style.overflow='hidden';
    history.pushState({{dish:1}},'');
  }}
  function close(back){{
    view.classList.remove('on'); view.setAttribute('aria-hidden','true');
    document.body.style.overflow='';
    if(back && history.state && history.state.dish) history.back();
  }}
  document.addEventListener('click',e=>{{
    const b=e.target.closest('.mopen'); if(!b||b.disabled) return;
    const meal=b.closest('.meal'); if(meal) open(meal);
  }});
  document.getElementById('dishback').addEventListener('click',()=>close(true));
  addEventListener('popstate',()=>{{ if(view.classList.contains('on')) close(false); }});
  addEventListener('keydown',e=>{{
    // Escape закрывает по одному слою за раз: если сверху открыто фото, оно уже
    // забрало это нажатие себе (см. preventDefault в обработчике фото).
    if(e.key==='Escape'&&!e.defaultPrevented&&view.classList.contains('on')) close(true);
  }});
}})();

// ── окно подписки ─────────────────────────────────────────────────────────
(function(){{
  const mo=document.getElementById('submodal'), lnk=document.getElementById('subsopen');
  if(!mo||!lnk) return;
  function open(){{
    mo.removeAttribute('hidden'); document.body.style.overflow='hidden';
    document.getElementById('subclose').focus();
    history.pushState({{sub:1}},'');
  }}
  function close(back){{
    mo.setAttribute('hidden',''); document.body.style.overflow='';
    if(back && history.state && history.state.sub) history.back();
  }}
  lnk.addEventListener('click',e=>{{e.preventDefault();open();}});
  document.getElementById('subclose').addEventListener('click',()=>close(true));
  // Клик по затемнению закрывает, по самому окну — нет.
  mo.addEventListener('click',e=>{{ if(!e.target.closest('.subwrap')) close(true); }});
  addEventListener('popstate',()=>{{ if(!mo.hasAttribute('hidden')) close(false); }});
  addEventListener('keydown',e=>{{
    if(e.key==='Escape'&&!e.defaultPrevented&&!mo.hasAttribute('hidden')){{close(true);e.preventDefault();}}
  }});
}})();

// PWA: service worker + install prompt
if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(()=>{{}});
let deferred=null; const ib=document.getElementById('install');
window.addEventListener('beforeinstallprompt',e=>{{e.preventDefault();deferred=e;ib.hidden=false;}});
ib.addEventListener('click',async()=>{{if(!deferred)return;deferred.prompt();await deferred.userChoice;deferred=null;ib.hidden=true;}});
window.addEventListener('appinstalled',()=>{{ib.hidden=true;}});
</script></body></html>"""


def menu_email_html(pl: dict, plan_link: str = "") -> str:
    """Письмо после оплаты: краткое меню (без рецептов) + кнопка на полный план в вебе."""
    # Подписи дней — те же, что на экране плана: письмо и экран не должны
    # расходиться в нумерации.
    days = "".join(_day_block({"day": _day_label(i), "meals": [
        (m.get("slot", ""), m.get("name", ""), m.get("kcal", "")) for m in (d.get("meals") or [])]})
        for i, d in enumerate(pl.get("days") or []))
    cta = _cta(plan_link, "Открыть план с рецептами") if plan_link else ""
    return (f"<div style='{_CSS_WRAP}'>"
            + _head("Твой план на 7 дней", "Спасибо за оплату! Меню — ниже, рецепты и список покупок — в плане")
            + _norm_card(pl) + cta
            + "<div style='padding:14px 24px 0;font-weight:800;font-size:16px'>Меню на неделю</div>"
            + days + _foot() + "</div>")

