"""Серверный расчёт персонального плана + генерация меню и HTML-писем.

Зеркалит расчёт из quiz.html (Mifflin-St Jeor). Меню — из курируемого банка блюд,
порции считаются от нормы калорий пользователя (30/40/30). Учитывает «без мяса».
"""
from __future__ import annotations

import html
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
    # Фото — кнопка: в списке оно 56px, а в кэше лежит 512px, и разглядеть блюдо
    # в такой марке невозможно. Тап открывает его на весь экран.
    img = (f"<button class='mimg' data-zoom='{_e(slug)}' data-name='{_e(name)}' "
           f"aria-label='Посмотреть фото: {_e(name)}'>"
           f"<img loading='lazy' data-slug='{_e(slug)}' alt='' "
           f"src='/dish/{quote(slug)}?t={quote(name)}'></button>")
    return (f"<div class='meal' data-k='{key}' data-kc='{kcnum}'{pfc}><div class='mrow'>{img}"
            f"<div class='minfo'><span class='slot'>{_e(m.get('slot',''))}</span>"
            f"<div class='mname'>{_e(name)}</div>{macros}</div>"
            f"<div class='kc'>{_e(kc)}<small>ккал</small></div></div>{details}"
            f"<div class='mact'><button class='done' data-k='{key}'><span class='dc'></span>Приготовил</button>"
            f"<button class='swap' data-day='{day}' data-slot='{slot}' data-i='{idx}' data-k='{key}'>Заменить</button>"
            f"<button class='dislike' data-name='{_e(name)}' title='Не нравится — убрать из меню'>"
            f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>"
            f"<path d='M17 2H7.3a2 2 0 0 0-2 1.7l-1.3 8A2 2 0 0 0 6 14h4l-.7 3.3a2 2 0 0 0 3.5 1.6L17 14'/>"
            f"<path d='M17 2h2a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2h-2'/></svg></button></div></div>")


def _shopping(sh: list, days: list | None = None) -> str:
    """Список покупок, по которому можно ходить по магазину.

    Было: статичная стена из 41 строки без единого элемента управления. С ней
    нельзя делать то, ради чего список нужен, — отмечать купленное; человек в
    магазине держит место в голове и сбивается.

    Стало: каждая позиция — переключатель, отметки живут локально по токену
    плана, сверху видно «куплено N из M».
    """
    if not sh:
        sh = _shopping_from_days(days or [])
    if not sh:
        return ""
    cats, total = "", 0
    for ci, c in enumerate(sh):
        items = ""
        for ii, i in enumerate(c.get("items") or []):
            total += 1
            items += (f"<li><label class='si'><input type='checkbox' data-si='{ci}-{ii}'>"
                      f"<span class='sb'></span><span class='st'>{_e(i)}</span></label></li>")
        if not items:
            continue
        n = len(c.get("items") or [])
        cats += (f"<div class='cat'><div class='ct'>{_e(c.get('cat',''))}<span>{n}</span></div>"
                 f"<ul>{items}</ul></div>")
    if not cats:
        return ""
    return (f"<section class='sec'><div class='shead'><h2>Список покупок</h2>"
            f"<button class='sclear' id='sclear' type='button'>Снять отметки</button></div>"
            f"<div class='sprog'><span id='sdone'>0</span> из {total} — куплено</div>"
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
    return (f"<section class='sec'><h2>Параметры плана</h2>"
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
    # ver/started пишет app.py при сохранении плана; у планов, созданных раньше, их нет —
    # тогда ведём себя как прежде. Фильтруем символы, потому что ver уезжает в JS-строку.
    ver = "".join(c for c in str(pl.get("ver") or "") if c.isalnum() or c in "-_.")
    week_over = ""
    if not _renews_weeks(sub):   # кому неделю пересоберёт cron — блок не показываем
        age = _days_since(pl.get("started") or "")
        if age is not None and age >= 7:
            week_over = _weekover(len(days), renew_link)
    tabs = "".join(f"<button class='tab{" on" if i==0 else ""}' data-d='{i}'>{i + 1}</button>"
                   for i, _ in enumerate(days))
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
        panels += (f"<div class='panel{" on" if i==0 else ""}' data-d='{i}'>"
                   f"<div class='dtitle'>{_day_label(i)} <span>{tot} ккал</span></div>"
                   f"<div class='calbar'><i class='calfill' data-d='{i}'></i></div>"
                   f"<div class='caltxt' data-d='{i}' data-tot='{tot}'>Съедено 0 из {tot} ккал</div>"
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
.norm,.streakc,.water,.meal,.wcard,.prefcard,.subcard,.si,.drow,.tips li,.wover{{
  background:var(--glass)!important;
  -webkit-backdrop-filter:blur(22px) saturate(165%);backdrop-filter:blur(22px) saturate(165%);
  border:1px solid var(--glass-line)!important;
  box-shadow:inset 0 1px 0 var(--glass-edge),inset 0 -1px 0 rgba(30,50,25,.05),var(--sh-2)!important}}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){{
  .norm,.streakc,.water,.meal,.wcard,.prefcard,.subcard,.si,.drow,.tips li,.wover{{background:var(--card)!important}}
}}
@media (prefers-reduced-transparency:reduce){{
  .aura{{display:none}}
  .norm,.streakc,.water,.meal,.wcard,.prefcard,.subcard,.si,.drow,.tips li,.wover{{
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
.norm{{border-radius:var(--rx);padding:21px;margin-top:16px}}
.norm .big{{font-family:Unbounded;font-weight:800;font-size:54px;color:var(--gd);
  letter-spacing:-.055em;line-height:.9}}
.norm .big small{{font-family:Onest;font-size:16px;color:var(--muted);font-weight:600;
  letter-spacing:-.01em;margin-left:9px}}
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
.tabs{{display:flex;gap:6px;overflow-x:auto;margin:22px 0 14px;position:sticky;top:52px;
  background:color-mix(in srgb,var(--bg) 86%,transparent);
  -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);padding:8px 0;z-index:4}}
.tab{{flex:0 0 auto;border:1px solid var(--glass-line);color:var(--muted);font-weight:700;font-size:14px;
  padding:9px 15px;border-radius:99px;cursor:pointer;font-family:inherit;
  background:var(--glass);-webkit-backdrop-filter:blur(18px) saturate(160%);backdrop-filter:blur(18px) saturate(160%);
  box-shadow:inset 0 1px 0 var(--glass-edge),var(--sh-1)}}
.tab.on{{background:var(--g);color:#fff;border-color:var(--g);box-shadow:var(--sh-1)}}
.panel{{display:none}}.panel.on{{display:block;animation:in .3s ease}}
@keyframes in{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
.dtitle{{font-weight:700;font-size:11px;margin:6px 4px 12px;text-transform:uppercase;letter-spacing:.11em;color:var(--muted)}}
.dtitle span{{color:var(--gd)}}
.meal{{border-radius:var(--rl);padding:14px;margin-bottom:10px}}
.mrow{{display:flex;justify-content:space-between;gap:12px;align-items:center}}
.mimg{{width:64px;height:64px;flex:0 0 auto;border-radius:13px;background:var(--soft);display:block;
  padding:0;border:none;overflow:hidden;cursor:zoom-in;position:relative}}
.mimg img{{width:100%;height:100%;object-fit:cover;display:block}}
/* Подсказка, что фото открывается: без неё картинка выглядит просто картинкой.
   Лупа мелкая и в углу, чтобы не спорить с самой едой. */
.mimg::after{{content:"";position:absolute;right:3px;bottom:3px;width:16px;height:16px;border-radius:50%;
  background:rgba(255,255,255,.9) url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23405038' stroke-width='2.4' stroke-linecap='round'><circle cx='10.5' cy='10.5' r='6.5'/><path d='M15.5 15.5 21 21'/></svg>") center/11px 11px no-repeat;
  box-shadow:0 1px 3px rgba(0,0,0,.25)}}

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
.minfo{{flex:1;min-width:0}}
.slot{{font-size:12px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}}
.mname{{font-size:16px;font-weight:700;margin-top:2px}}.mm{{font-size:12px;color:var(--muted)}}
.kc{{font-weight:800;color:var(--gd);white-space:nowrap;font-size:15px}}.kc small{{font-size:11px;color:var(--muted);margin-left:2px}}
details{{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}}
summary{{font-size:13px;font-weight:700;color:var(--gd);cursor:pointer}}
.dh{{font-size:12px;font-weight:800;color:var(--muted);text-transform:uppercase;margin:10px 0 4px}}
.ing,.steps{{padding-left:18px;font-size:14px;line-height:1.6}}.steps li{{margin-bottom:4px}}
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
.sprog{{color:var(--muted);font-size:13px;font-weight:700;margin:-6px 0 12px}}
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
  font-size:14px;padding:11px;border-radius:12px;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:9px}}
.swap{{flex:0 0 auto;border:1.5px solid var(--line);background:var(--card);color:var(--muted);font-weight:700;
  font-size:14px;padding:11px 16px;border-radius:12px;cursor:pointer}}
.swap:hover{{border-color:var(--g);color:var(--gd)}}.swap:disabled{{opacity:.5}}
/* «Не нравится». У кнопки не было НИ ОДНОГО правила: инлайновый svg без
   размеров сплющивал её в вертикальную чёрточку справа от «Заменить» — на
   экране это читалось как случайный артефакт вёрстки, а не как кнопка.
   Делаем квадратной под высоту соседей и задаём размер иконке. */
.dislike{{flex:0 0 auto;width:44px;border:1.5px solid var(--line);background:var(--card);
  color:var(--muted);border-radius:12px;cursor:pointer;display:flex;align-items:center;
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
.meal.on .done{{border-color:var(--g);background:var(--soft);color:var(--gd)}}
.meal.on .done .dc{{background:var(--g);border-color:var(--g)}}
.meal.on .done .dc::after{{content:"";position:absolute;left:4px;top:1px;width:6px;height:10px;border:2px solid #fff;border-top:0;border-left:0;transform:rotate(45deg)}}
.meal.on .done::after{{content:" ✓"}}
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
.calbar{{height:7px;background:var(--line);border-radius:99px;overflow:hidden;margin:0 0 6px}}
.calfill{{display:block;height:100%;width:0;background:var(--g);border-radius:99px;transition:width .35s ease}}
.calfill.over{{background:#E0912B}}
.caltxt{{font-size:12px;color:var(--muted);font-weight:600;margin-bottom:14px}}
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
.wspark{{width:100%;height:80px;margin:14px 0 4px;display:block}}
.wspark path{{fill:none;stroke:var(--g);stroke-width:2.5;stroke-linejoin:round;stroke-linecap:round}}
.wspark circle{{fill:var(--g)}}
.wadd{{display:flex;gap:8px;margin-top:12px}}
.wadd input{{flex:1;min-width:0;border:1.5px solid var(--line);border-radius:12px;padding:12px 14px;font-size:16px;background:var(--bg);color:var(--ink)}}
.wadd button{{border:none;border-radius:12px;background:var(--g);color:#fff;font-weight:700;font-size:15px;padding:0 18px;cursor:pointer;white-space:nowrap}}
.whint{{font-size:12px;color:var(--muted);margin-top:9px;line-height:1.4}}
.prefcard{{border-radius:var(--rx);padding:18px}}
.params{{padding:6px 18px}}
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
    <div class="norm"><div class="big">{pl.get('cal','')}<small> ккал/день</small></div>
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
    <h1>Я</h1><p class="lead">Вес, вкусы и подписка</p>
  <section class="sec" id="weightsec"><h2>Твой вес</h2>
    <div class="wcard">
      <div class="wrow"><div class="wbig"><span id="wcur">—</span><small>кг</small></div>
        <div class="wdelta" id="wdelta"></div></div>
      <svg class="wspark" id="wspark" viewBox="0 0 300 80" preserveAspectRatio="none"></svg>
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
  {_params(pl, goal_code, water_goal)}
  {acct}
  {_tips(pl.get('tips') or [])}
  <footer class="plegal">
    <div class="plinks"><a href="/offer">Оферта</a><a href="/privacy">Политика ПДн</a><a href="/consent">Согласие</a><a href="/login">Войти по почте</a></div>
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

<!-- Просмотр дня недели. Отдельный экран, а не всплывашка: меню дня — это пять
     карточек с фото, шторка съела бы половину. Действий нет намеренно. -->
<section class="dayview" id="dayview" aria-hidden="true">
  <div class="dvtop"><button id="dvback" aria-label="Назад">{_ic_back()}</button><b id="dvtitle"></b></div>
  <div class="dvbody" id="dvbody"></div>
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
  const cur=document.querySelector('.tab.on');
  document.querySelectorAll('.drow .dnow').forEach(e=>{{
    e.hidden = !cur || e.closest('.drow').dataset.d !== cur.dataset.d;
  }});
}}
(function(){{const m=location.hash.match(/d(\\d+)/);
  if(m){{activateDay(parseInt(m[1]));markToday();return;}}
  let wd=(new Date().getDay()+6)%7; if(wd>=NDAYS) wd=0; activateDay(wd); markToday();}})();
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
    const els=[...document.querySelectorAll(".panel[data-d='"+p[0]+"'] .meal")]
      .filter(el=>(el.dataset.k||'').split(':').slice(2).join(':')===p[1]);
    delete o[k]; ch=true;
    if(els.length===1) o[els[0].dataset.k]=1;
  }});
  return ch;
}}
let done=JSON.parse(localStorage.getItem(DKEY)||'{{}}');
if(migrateDone(done)) localStorage.setItem(DKEY,JSON.stringify(done));
function dayComplete(i){{const ks=[...document.querySelectorAll(".panel[data-d='"+i+"'] .meal")].map(el=>el.dataset.k);return ks.length>0 && ks.every(k=>done[k]);}}
function paint(){{
  document.querySelectorAll('.meal').forEach(el=>el.classList.toggle('on', !!done[el.dataset.k]));
  const tabs=document.querySelectorAll('.tab'); let comp=0, run=0, best=0;
  for(let i=0;i<tabs.length;i++){{const c=dayComplete(i); tabs[i].classList.toggle('complete',c);
    if(c){{comp++;run++;best=Math.max(best,run);}} else run=0;}}
  const pr=document.getElementById('prog'); if(pr) pr.textContent=comp+' / '+tabs.length;
  // Ряд огоньков: закрытые дни цветные. Рисуем по ТЕМ ЖЕ отметкам, что и число,
  // иначе плитка начнёт противоречить сама себе.
  const fl=document.getElementById('flames');
  if(fl) fl.innerHTML=Array.from({{length:tabs.length}},(_,i)=>
    '<span class="'+(dayComplete(i)?'':'off')+'"><img src="/assets/streak-flame.svg" alt="" loading="lazy"></span>').join('');
  const wo=document.getElementById('woDone'); if(wo) wo.textContent=comp;   // итог недели
  const sm=document.getElementById('streakmsg');
  if(sm) sm.innerHTML = best>=2 ? ('Серия <b>'+best+'</b> дней подряд — так держать!')
    : comp>0 ? 'Отличное начало! Не бросай серию' : 'Отмечай «Приготовил» — собери серию';
  document.querySelectorAll('.caltxt').forEach(tx=>{{
    const di=tx.dataset.d, tot=+tx.dataset.tot||0; let eaten=0;
    document.querySelectorAll(".panel[data-d='"+di+"'] .meal").forEach(el=>{{ if(done[el.dataset.k]) eaten+=(+el.dataset.kc||0); }});
    tx.textContent='Съедено '+eaten+' из '+tot+' ккал';
    const f=document.querySelector(".calfill[data-d='"+di+"']");
    if(f){{ f.style.width=(tot?Math.min(100,Math.round(eaten/tot*100)):0)+'%'; f.classList.toggle('over',eaten>tot*1.05); }}
  }});
  // БЖУ: набрано из нормы по ОТКРЫТОМУ дню — по тем же отметкам, что и калории.
  ['P','F','C'].forEach(m=>{{
    const num=document.getElementById('got'+m), bar=document.getElementById('bar'+m);
    if(!num||!bar) return;
    let got=0;
    document.querySelectorAll('.panel.on .meal').forEach(el=>{{
      if(done[el.dataset.k]) got+=(+el.dataset[m.toLowerCase()]||0);
    }});
    const goal=parseInt(num.parentNode.querySelector('i').textContent.replace(/\\D/g,''),10)||0;
    num.textContent=got;
    bar.style.width=(goal?Math.min(100,Math.round(got/goal*100)):0)+'%';
  }});
}}
document.querySelectorAll('.done').forEach(b=>b.addEventListener('click',()=>{{
  const k=b.dataset.k; if(done[k])delete done[k]; else done[k]=1;
  localStorage.setItem(DKEY,JSON.stringify(done)); paint(); pushProgress();
}}));
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
}}
renderWater();
// вес + тренд
const WTK='np_wt_'+T, MN=['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
const fmtd=s=>{{const d=new Date(s);return d.getDate()+' '+MN[d.getMonth()];}};
const loadWt=()=>{{try{{return JSON.parse(localStorage.getItem(WTK)||'[]');}}catch(e){{return[];}}}};
function drawSpark(pts){{const svg=document.getElementById('wspark');if(!svg)return;
  if(pts.length<1){{svg.innerHTML='';return;}}
  const ws=pts.map(p=>p.w);let mn=Math.min(...ws),mx=Math.max(...ws);if(mx-mn<1){{mn-=1;mx+=1;}}
  const W=300,H=80,pad=10,xf=i=>pts.length<2?W/2:pad+i*(W-2*pad)/(pts.length-1),yf=w=>H-pad-(w-mn)/(mx-mn)*(H-2*pad);
  let d='';pts.forEach((p,i)=>{{d+=(i?'L':'M')+xf(i).toFixed(1)+' '+yf(p.w).toFixed(1)+' ';}});
  const dots=pts.map((p,i)=>"<circle cx='"+xf(i).toFixed(1)+"' cy='"+yf(p.w).toFixed(1)+"' r='3'/>").join('');
  svg.innerHTML="<path d='"+d+"'/>"+dots;
}}
function renderWeight(){{
  const a=loadWt(),base=START_W>0?START_W:(a[0]?a[0].w:0),cur=a.length?a[a.length-1].w:(START_W>0?START_W:0);
  const ce=document.getElementById('wcur');if(ce)ce.textContent=cur?String(cur).replace('.',','):'—';
  const de=document.getElementById('wdelta');
  if(de){{if(cur&&base){{const diff=Math.round((cur-base)*10)/10;
    if(Math.abs(diff)<0.05){{de.textContent='±0 кг';de.className='wdelta';}}
    else{{const good=GOAL==='gain'?diff>0:GOAL==='lose'?diff<0:true;
      de.textContent=(diff>0?'+':'')+String(diff).replace('.',',')+' кг';de.className='wdelta '+(good?'g':'b');}}
  }}else de.textContent='';}}
  drawSpark((START_W>0?[{{d:'',w:START_W}}]:[]).concat(a));
  const h=document.getElementById('whint');
  if(h)h.textContent=a.length?('Записей: '+a.length+' · последняя '+fmtd(a[a.length-1].d)):
    (START_W>0?('Старт из анкеты — '+String(START_W).replace('.',',')+' кг. Записывай раз в неделю — увидишь тренд.'):'Записывай вес раз в неделю — увидишь тренд.');
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
document.querySelectorAll('.swap').forEach(b=>b.addEventListener('click',async()=>{{
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
}}));
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
document.querySelectorAll('.dislike').forEach(b=>{{let armed=false;
  // Подсказку рисуем строкой в самой строке действий: title на телефоне не
  // виден, а без обратной связи первый тап выглядит как «ничего не произошло».
  const hint=document.createElement('div');hint.className='hintx';
  hint.textContent='Нажми ещё раз — уберу это блюдо и пересоберу план';
  b.parentNode.appendChild(hint);
  b.addEventListener('click',async()=>{{
  if(!armed){{armed=true;b.classList.add('armed');hint.classList.add('show');
    b.title='Нажми ещё раз — уберу это блюдо и пересоберу план';
    setTimeout(()=>{{armed=false;b.classList.remove('armed');hint.classList.remove('show');
      b.title='Не нравится — убрать из меню';}},4000);return;}}
  const name=b.dataset.name||''; document.querySelectorAll('.dislike').forEach(x=>x.disabled=true);
  try{{
    const r=await fetch('/api/plan/'+T+'/settings',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{dislike:name}})}});
    const j=await r.json();if(!j.ok)throw 0; location.hash=''; location.reload();
  }}catch(e){{document.querySelectorAll('.dislike').forEach(x=>x.disabled=false);alert('Не удалось обновить меню — попробуй ещё раз');}}
}});}});
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
    document.body.style.overflow='';
    if(opener){{opener.focus();opener=null;}}     // возвращаем фокус туда, откуда открыли
  }}
  document.addEventListener('click',e=>{{
    const b=e.target.closest('.mimg[data-zoom]');
    if(b){{open(b.dataset.zoom,b.dataset.name,b);return;}}
    // Клик по фону и по кресту закрывают; по самой картинке — нет.
    if(lb.classList.contains('on') && !e.target.closest('figure')) close();
  }});
  document.addEventListener('keydown',e=>{{if(e.key==='Escape'&&lb.classList.contains('on'))close();}});
}})();

// Список покупок: отметки купленного. Раньше это была стена текста без единого
// элемента управления — то есть с ним нельзя было делать ровно то, ради чего
// список и нужен. Отметки держим локально: они про поход в магазин, а не про
// данные аккаунта, и синхронизировать их между устройствами незачем.
(function(){{
  const box=document.getElementById('shop'); if(!box) return;
  const KEY='np_shop_'+T, out=document.getElementById('sdone');
  const prog=document.querySelector('.sprog');
  const total=parseInt(box.dataset.total||'0');
  let st={{}}; try{{st=JSON.parse(localStorage.getItem(KEY)||'{{}}');}}catch(e){{}}
  const boxes=[...box.querySelectorAll('input[data-si]')];
  function paint(){{
    const n=boxes.filter(b=>b.checked).length;
    if(out) out.textContent=n;
    if(prog) prog.classList.toggle('all', total>0 && n===total);
  }}
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
  paint();
}})();

// ── вкладки ──────────────────────────────────────────────────────────────
// Разделы те же, что были в свитке; переключаем видимость контейнеров.
document.querySelectorAll('.bnav button').forEach(b=>b.addEventListener('click',()=>{{
  document.querySelectorAll('.bnav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('.scr').forEach(s=>s.classList.toggle('on',s.id==='sc-'+b.dataset.s));
  window.scrollTo(0,0);
}}));

// ── просмотр дня недели (только посмотреть) ───────────────────────────────
// Карточки берём из уже отрисованных панелей и снимаем всё, чем можно
// ДЕЙСТВОВАТЬ: отмечать и заменять — только на «Сегодня», иначе человек легко
// закроет чужой день и собьёт себе прогресс.
(function(){{
  const view=document.getElementById('dayview'), body=document.getElementById('dvbody');
  if(!view) return;
  function open(i){{
    const panel=document.querySelector('.panel[data-d="'+i+'"]');
    if(!panel) return;
    const clone=panel.cloneNode(true);
    clone.querySelectorAll('.mact, details').forEach(n=>n.remove());
    clone.querySelectorAll('.calbar, .caltxt').forEach(n=>n.remove());
    clone.querySelectorAll('[id]').forEach(n=>n.removeAttribute('id'));   // без дублей id
    clone.querySelectorAll('.meal').forEach(n=>{{n.classList.remove('on');n.removeAttribute('data-k');}});
    const title=(clone.querySelector('.dtitle')||{{}}).textContent||('День '+(i+1));
    const t=clone.querySelector('.dtitle'); if(t) t.remove();
    document.getElementById('dvtitle').textContent=title.trim().replace(/\\s+(\\d+\\s*ккал)$/,' · $1');
    body.innerHTML='<div class="dvnote">Только просмотр. Отмечать и заменять блюда можно на вкладке «Сегодня».</div>';
    body.appendChild(clone);
    clone.classList.add('on');
    view.classList.add('on'); view.setAttribute('aria-hidden','false');
    view.scrollTop=0; document.body.style.overflow='hidden';
    history.pushState({{dayview:1}},'');    // системное «назад» закрывает просмотр
  }}
  function close(back){{
    view.classList.remove('on'); view.setAttribute('aria-hidden','true');
    document.body.style.overflow='';
    if(back && history.state && history.state.dayview) history.back();
  }}
  document.querySelectorAll('.drow').forEach(r=>r.addEventListener('click',()=>open(+r.dataset.d)));
  document.getElementById('dvback').addEventListener('click',()=>close(true));
  addEventListener('popstate',()=>{{ if(view.classList.contains('on')) close(false); }});
  addEventListener('keydown',e=>{{ if(e.key==='Escape'&&view.classList.contains('on')) close(true); }});
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

