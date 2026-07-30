"""
Nutrition — скелет бэкенда под тест 4 лендингов (см. STRUCTURE.md).

Один продукт (персональный план питания), 8 лендингов под разные углы. На каждый
льём отдельный трафик, сравниваем конверсию. Движок квиза/лида/статистики
переиспользуется из astro + sdelka. Оплата БОЕВАЯ (ЮKassa, shop 1411445): разовый
299₽ + подписка 499₽/мес с автосписанием (рекурренты одобрены 2026-07-22).
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

import dish_photos

app = FastAPI(title="Nutrition", docs_url=None, redoc_url=None)

# ── Режим «не считать меня» ──────────────────────────────────────────────────
# Свои проходы по боевому сайту пачкают ровно те цифры, ради которых сайт и
# мерят. Один прогон e2e по проду добавил 2 живых лида и девять заходов в квиз,
# и их пришлось вычищать руками — этого не должно повторяться.
#
# Включается один раз через /?notrack=1 (на любом маршруте), дальше живёт в
# куке. Отключается через /?notrack=0. Пока включён: счётчики не растут, лиды
# не пишутся, письма не уходят и счётчик Метрики на страницу не встаёт — то
# есть нас не видит ни своя статистика, ни Яндекс.
NOTRACK_COOKIE = "np_notrack"
_notrack: ContextVar[bool] = ContextVar("notrack", default=False)


@app.middleware("http")
async def _notrack_mw(request: Request, call_next):
    q = request.query_params.get("notrack")
    on = request.cookies.get(NOTRACK_COOKIE) == "1"
    if q == "1":
        on = True
    elif q == "0":
        on = False
    token = _notrack.set(on)
    try:
        response = await call_next(request)
    finally:
        _notrack.reset(token)
    if q == "1":
        response.set_cookie(NOTRACK_COOKIE, "1", max_age=31_536_000, samesite="lax", path="/")
    elif q == "0":
        response.delete_cookie(NOTRACK_COOKIE, path="/")
    return response


def notrack() -> bool:
    return _notrack.get()


STATIC = Path(__file__).parent / "static"
# картинки контента (Nano Banana) + прочие статик-ассеты лендингов
(STATIC / "assets").mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(STATIC / "assets")), name="assets")
DATA = Path(os.getenv("DATA_DIR", "/app/data"))
LEADS = DATA / "leads.jsonl"
COUNTERS = DATA / "counters.json"
ORDERS = DATA / "orders.jsonl"  # PII — только в DATA (gitignore), не коммитить
PLANS = DATA / "plans"          # сгенерированные планы: {token}.json

# ЮKassa (ключи только из .env боевого сервера; без них — оплата отдаёт 503)
YOOKASSA_SHOP = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
PRICE_RUB = os.getenv("NUTRI_PRICE_RUB", "299")          # разовый план
SUB_PRICE_RUB = os.getenv("NUTRI_SUB_PRICE_RUB", "499")  # подписка / мес
CRON_SECRET = os.getenv("NUTRI_CRON_SECRET", "").strip() # защита cron-эндпоинта
SUBS = DATA / "subs"                                       # подписки: {sub_id}.json

# Мейлер (Unisender Go). Без NUTRI_MAIL_FROM + ключа отправка просто пропускается.
NUTRI_MAIL_FROM = os.getenv("NUTRI_MAIL_FROM", "").strip()
NUTRI_MAIL_FROM_NAME = os.getenv("NUTRI_MAIL_FROM_NAME", "NutriPlan")


def _send_email(to: str, subject: str, html: str, tag: str = "") -> dict:
    """Транзакционное письмо через Unisender Go (самодостаточно, urllib).
    Результат ЛОГИРУЕТСЯ (раньше молча терялся — план мог не дойти без следа)."""
    key = os.getenv("UNISENDER_GO_API_KEY", "").strip()
    if not (key and NUTRI_MAIL_FROM):
        _bump("mail_skipped")
        print(f"[ALERT] mail NOT configured — письмо '{tag or subject}' НЕ отправлено на {to}", flush=True)
        return {"skipped": "mail not configured"}
    res = _send_email_raw(key, to, subject, html, tag)
    if res.get("error") or (isinstance(res, dict) and res.get("status") == "error"):
        _bump("mail_fail")
        print(f"[ALERT] mail send failed tag={tag} to={to}: {str(res)[:160]}", flush=True)
    return res


def _send_email_raw(key: str, to: str, subject: str, html: str, tag: str = "") -> dict:
    import urllib.request
    msg = {"recipients": [{"email": to}], "subject": subject,
           "from_email": NUTRI_MAIL_FROM, "from_name": NUTRI_MAIL_FROM_NAME, "body": {"html": html}}
    if tag:
        msg["tags"] = [tag]
    rt = os.getenv("NUTRI_MAIL_REPLY_TO", "").strip()
    if rt:
        msg["reply_to"] = rt
    url = os.getenv("UNISENDER_GO_SEND_URL",
                    "https://go2.unisender.ru/ru/transactional/api/v1/email/send.json")
    req = urllib.request.Request(url, data=json.dumps({"message": msg}).encode(),
                                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def _save_plan(token: str, pl: dict, reset_progress: bool = False) -> None:
    try:
        PLANS.mkdir(parents=True, exist_ok=True)
        (PLANS / f"{token}.json").write_text(json.dumps(pl, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if reset_progress:  # новая версия плана → блюда не должны быть уже «приготовлены»
        try:
            pr = _load_progress(token)
            if pr.get("done"):
                pr["done"] = {}  # воду/вес сохраняем — это трекеры, а не отметки блюд
                _save_progress(token, pr)
        except Exception:  # noqa: BLE001
            pass


def _load_plan(token: str) -> dict:
    token = "".join(c for c in token if c.isalnum())  # без path-traversal
    try:
        f = PLANS / f"{token}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


# ---------- серверный прогресс (стрик/вода/вес), привязан к токену плана ----------

PROGRESS = DATA / "progress"  # {token}.json = {done, water, weight}


def _load_progress(token: str) -> dict:
    token = "".join(c for c in token if c.isalnum())
    try:
        f = PROGRESS / f"{token}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def _sanitize_progress(inc: dict) -> dict:
    """Нормализовать входящее состояние. Клиент — источник истины (replace, а не union):
    так работают отмена «съеденного» и очистка отметки при замене блюда. Начальное
    объединение локального+серверного делается на клиенте один раз при загрузке."""
    done = {str(k): 1 for k, v in (inc.get("done") or {}).items() if v}
    water = {}
    for d, n in (inc.get("water") or {}).items():
        try:
            water[str(d)] = int(n)
        except Exception:  # noqa: BLE001
            pass
    weight = []
    for e in (inc.get("weight") or []):
        if isinstance(e, dict) and e.get("d"):
            weight.append({"d": e["d"], "w": e.get("w")})
    weight.sort(key=lambda e: e.get("d") or "")
    return {"done": done, "water": water, "weight": weight}


def _save_progress(token: str, data: dict) -> None:
    token = "".join(c for c in token if c.isalnum())
    if not token:
        return
    try:
        PROGRESS.mkdir(parents=True, exist_ok=True)
        (PROGRESS / f"{token}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


@app.get("/api/plan/{token}/progress")
def get_progress(token: str) -> JSONResponse:
    return JSONResponse(_load_progress(token) or {"done": {}, "water": {}, "weight": []})


@app.post("/api/plan/{token}/progress")
async def post_progress(token: str, request: Request) -> JSONResponse:
    tok = "".join(c for c in token if c.isalnum())
    if not (tok and (PLANS / f"{tok}.json").exists()):   # прогресс только для реального плана
        return JSONResponse({"error": "no plan"}, status_code=404)
    try:
        inc = await request.json()
    except Exception:  # noqa: BLE001
        inc = {}
    inc = inc if isinstance(inc, dict) else {}
    data = _sanitize_progress(inc)
    try:
        data["_t"] = int(inc.get("t") or 0)  # клиентская версия (Date.now)
    except Exception:  # noqa: BLE001
        data["_t"] = 0
    prev = _load_progress(tok)
    # Версионная защита от кросс-девайс затирания: если входящее СТАРЕЕ сохранённого —
    # не заменяем, а доливаем (union done, max воды, объединение веса по дате). Свежее (t>=)
    # заменяет полностью (клиент авторитетен для отмены/очистки на своём устройстве).
    if prev and int(prev.get("_t") or 0) > data["_t"]:
        done = {**(prev.get("done") or {}), **(data.get("done") or {})}
        water = dict(prev.get("water") or {})
        for d, n in (data.get("water") or {}).items():
            water[d] = max(int(water.get(d, 0)), int(n))
        wt = {e["d"]: e for e in (data.get("weight") or []) if e.get("d")}
        for e in (prev.get("weight") or []):
            if e.get("d"):
                wt[e["d"]] = e  # сохранённое (новее) в приоритете
        data = {"done": done, "water": water,
                "weight": sorted(wt.values(), key=lambda e: e.get("d") or ""),
                "_t": prev.get("_t")}
    _save_progress(tok, data)
    return JSONResponse(data)


def _send_lead(email: str, quiz: dict, link: str = "") -> None:
    try:
        import plan
        _send_email(email, "Твоя норма калорий готова + пример дня · NutriPlan",
                    plan.lead_html(quiz, link), "plan_lead")
    except Exception:  # noqa: BLE001
        pass


def _fulfill_paid(email: str, quiz: dict, oid: str, base: str) -> None:
    """После оплаты: сгенерировать план, сохранить, отправить письмо со ссылкой на веб-план.
    Работает ДАЖЕ при пустом quiz (generate_plan({}) даёт валидный bank-план) — оплаченный
    клиент никогда не остаётся без плана; при пустом quiz шумно логируем алерт."""
    if not quiz:
        _bump("fulfill_no_quiz")  # алерт: оплата без квиза (потерян заказ) — план всё равно отдадим
        print(f"[ALERT] fulfill without quiz: order={oid} email={email}", flush=True)
    try:
        import plan
        import plan_ai
        pl = plan_ai.generate_plan(quiz or {})
        pl["quiz"] = quiz or {}  # нужно для замены блюд
        if oid:
            _save_plan(oid, pl)
        link = f"{base}/plan/{oid}" if oid else ""
        _send_email(email, "Твой план на 7 дней · NutriPlan", plan.menu_email_html(pl, link), "plan_paid")
        _pregen_dish_photos(pl)  # фото блюд заранее — к открытию плана уже готовы (общий кэш)
    except Exception as e:  # noqa: BLE001
        _bump("fulfill_fail")
        print(f"[ALERT] fulfill failed: order={oid} err={e}", flush=True)


def _pregen_dish_photos(pl: dict) -> None:
    """Прогреть общий кэш фото для всех блюд плана (идемпотентно, best effort)."""
    try:
        seen: set[str] = set()
        for d in pl.get("days") or []:
            for m in d.get("meals") or []:
                name = m.get("name", "")
                slug = dish_photos.slugify(name)
                if slug and slug not in seen and not dish_photos.has_photo(slug):
                    seen.add(slug)
                    dish_photos.generate(slug, name)
    except Exception:  # noqa: BLE001
        pass


# ---------- подписки (рекуррент ЮKassa) ----------

def _sub_id_safe(sid: str) -> str:
    return "".join(c for c in sid if c.isalnum())


def _save_sub(sub: dict) -> None:
    try:
        SUBS.mkdir(parents=True, exist_ok=True)
        (SUBS / f"{_sub_id_safe(sub['sub_id'])}.json").write_text(json.dumps(sub, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _load_sub(sid: str) -> dict:
    try:
        f = SUBS / f"{_sub_id_safe(sid)}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def _sub_merge(sid: str, fields: dict, remove: tuple = ()) -> None:
    """Перечитать подписку с диска и записать ТОЛЬКО указанные поля — не затирая
    параллельные изменения (cancel/unbind могли пройти, пока cron думал минуты).
    Фикс гонки cron vs cancel/unbind."""
    cur = _load_sub(sid)
    if not cur:
        return
    cur.update(fields)
    for k in remove:
        cur.pop(k, None)
    _save_sub(cur)


def _guarded_status(sid: str, new_status: str) -> bool:
    """Сменить статус подписки ТОЛЬКО если она ещё active (не затираем отмену/завершение,
    случившиеся параллельно, пока cron думал). Возвращает True, если применено."""
    cur = _load_sub(sid)
    if not cur or cur.get("status") != "active":
        return False
    _sub_merge(sid, {"status": new_status})
    return True


def _sub_ended_email(base: str, unbound: bool) -> str:
    head = ("Подписка завершена" if unbound else "Подписка приостановлена")
    body = ("Автопродление отключено (ты отвязал карту) — доступ к плану сохранён. "
            "Хочешь снова получать свежие планы каждую неделю? Оформи подписку заново:"
            if unbound else
            "Не получилось продлить подписку — банк не подтвердил списание. Твой план сохранён. "
            "Оформи подписку заново, чтобы продолжить получать свежие планы:")
    return (f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;"
            f"padding:32px;color:#20321F'><h2 style='margin:0 0 12px'>{head}</h2>"
            f"<p style='color:#6B7566'>{body}</p>"
            f"<p style='margin:20px 0'><a href='{base}/quiz' style='display:inline-block;background:#16A34A;"
            f"color:#fff;text-decoration:none;font-weight:800;padding:15px 26px;border-radius:14px'>"
            f"Возобновить подписку</a></p></div>")


_PROCESSED = DATA / "processed_payments.json"


def _already_processed(pid: str) -> bool:
    """Идемпотентность вебхука: True если этот payment_id уже обрабатывали (повторная
    доставка от ЮKassa) → второй раз ничего не делаем (не сбрасываем next_charge, не шлём
    второе письмо, не дублируем LLM). Атомарно помечаем при первом заходе."""
    if not pid:
        return False
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        seen = json.loads(_PROCESSED.read_text(encoding="utf-8")) if _PROCESSED.exists() else {}
    except Exception:  # noqa: BLE001
        seen = {}
    if pid in seen:
        return True
    seen[pid] = datetime.now(timezone.utc).isoformat()
    if len(seen) > 5000:  # не растим бесконечно — режем старые
        seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-3000:])
    try:
        _PROCESSED.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return False


# Демо-планы для проверки ЮKassa — самовосстанавливаются в исходное состояние
# при каждом открытии страницы (проверяющий может жать «Отвязать/Отменить» сколько
# угодно, при перезагрузке всё снова «Активна + карта привязана»).
DEMO_TOKENS = {t for t in os.getenv("NUTRI_DEMO_TOKENS", "sample").split(",") if t}


def _demo_sub(token: str) -> dict:
    now = datetime.now(timezone.utc)
    return {"sub_id": token, "status": "active", "payment_method_id": "demo-card-pm",
            "amount": SUB_PRICE_RUB, "email": "demo@mynutriplan.ru", "landing": "slim",
            "plan_token": token, "has_card": True,
            "next": (now + timedelta(days=30)).isoformat(),
            "next_charge": (now + timedelta(days=30)).isoformat(),
            "created": now.isoformat()}


def _all_subs():
    if not SUBS.exists():
        return
    for f in SUBS.glob("*.json"):
        try:
            yield json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue


def _charge_subscription(sub: dict) -> tuple[str, str]:
    """Автосписание по сохранённому способу оплаты (рекуррент).
    Возвращает (статус, payment_id): 'succeeded' — списано синхронно;
    'pending' — платёж создан, ждём webhook (НЕ провал!); 'failed' — не удалось."""
    if not (YOOKASSA_SHOP and YOOKASSA_SECRET and sub.get("payment_method_id")):
        return "failed", ""
    import hashlib
    import requests
    val = f"{float(sub.get('amount', SUB_PRICE_RUB)):.2f}"
    body = {
        "amount": {"value": val, "currency": "RUB"}, "capture": True,
        "payment_method_id": sub["payment_method_id"],
        "description": "NutriPlan — продление подписки",
        "metadata": {"type": "sub_renew", "order": sub["sub_id"], "email": sub.get("email", "")},
        "receipt": {"customer": {"email": sub.get("email", "")}, "items": [{
            "description": "Подписка NutriPlan (1 месяц)", "quantity": "1.00",
            "amount": {"value": val, "currency": "RUB"},
            "vat_code": 1, "payment_subject": "service", "payment_mode": "full_payment"}]},
    }
    # ДЕТЕРМИНИРОВАННЫЙ ключ на (подписка + период): любой ретрай в этом же месяце (после
    # таймаута, повторного тика) вернёт ТОТ ЖЕ платёж, а не создаст второй → нет двойного
    # списания даже если payment_id потерялся на сетевом сбое.
    idem = hashlib.sha256(f"{sub['sub_id']}|{sub.get('next_charge','')}".encode()).hexdigest()
    try:
        r = requests.post("https://api.yookassa.ru/v3/payments", auth=(YOOKASSA_SHOP, YOOKASSA_SECRET),
                          headers={"Idempotence-Key": idem, "Content-Type": "application/json"},
                          json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
        st = j.get("status")
        if st == "succeeded":
            return "succeeded", j.get("id", "")
        if st in ("pending", "waiting_for_capture"):
            return "pending", j.get("id", "")
        return "failed", j.get("id", "")
    except Exception:  # noqa: BLE001
        return "failed", ""


def _yk_payment_status(payment_id: str) -> str:
    """Статус платежа в ЮKassa (для досмотра pending-продлений)."""
    try:
        import requests
        r = requests.get(f"https://api.yookassa.ru/v3/payments/{payment_id}",
                         auth=(YOOKASSA_SHOP, YOOKASSA_SECRET), timeout=20)
        r.raise_for_status()
        return r.json().get("status", "")
    except Exception:  # noqa: BLE001
        return ""


def _advance_charge(sub: dict, now: datetime) -> bool:
    """Сдвинуть next_charge на +30д, но ТОЛЬКО если он ещё в прошлом (guard от
    двойного сдвига: cron и webhook могут оба узнать об успехе одного платежа)."""
    try:
        nc = datetime.fromisoformat(sub["next_charge"])
    except Exception:  # noqa: BLE001
        return False
    if nc > now:
        return False  # уже сдвинут другим путём
    sub["next_charge"] = (nc + timedelta(days=30)).isoformat()
    return True


def _find_order(oid: str) -> dict:
    """Найти запись СОЗДАНИЯ заказа (с quiz) по order id. Битая строка не должна ронять
    поиск последующих (конкурентные append из webhook+pay_create без лока) → per-line try."""
    best = {}
    try:
        for line in ORDERS.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue  # битая строка — пропускаем, не обрываем цикл
            if rec.get("order") != oid:
                continue
            if rec.get("quiz"):
                return rec        # запись создания с квизом — то, что нужно
            best = best or rec    # хотя бы какой-то заказ с этим oid (для суммы/email)
    except Exception:  # noqa: BLE001
        pass
    return best

# 4 угла позиционирования. Каждый = свой лендинг + своя кампания Директа.
LANDINGS = {
    "fitness": "Спорт/форма — питание под тренировки",
    "slim":    "Похудение мягко — без голода",
    "energy":  "Здоровье и энергия — наладить питание",
    "easy":    "Без готовки — меню + список покупок",
    "pro":     "Система — питание как проект (тёмный/атлетик)",
    "chef":    "Готовим красиво — кулинарный журнал",
    "coach":   "Твой коуч — план в кармане (app)",
    "reset":   "Мягкий рестарт — отношения с едой (пастель)",
}
DEFAULT_LANDING = "slim"

# Палитра квиза под каждый лендинг (совпадает с основной кнопкой сайта).
# accent — основной цвет, soft — светлая подложка выбранного/трека, cta — цвет текста на кнопке.
THEME = {
    "slim":    dict(accent="#16A34A", accentD="#0E7A36", soft="#E7F8EC", muted="#6B7566", ink="#20321F", bg="#FBF8F1", field="#FFFFFF", line="#EEE7D8", cta="#FFFFFF", dark=False),
    "fitness": dict(accent="#16A34A", accentD="#0F7A37", soft="#DCFCE7", muted="#5F6F64", ink="#16241C", bg="#FBF9F4", field="#FFFFFF", line="#ECE7DB", cta="#FFFFFF", dark=False),
    "energy":  dict(accent="#16A34A", accentD="#0F7A37", soft="#DCFCE7", muted="#5F6F64", ink="#16241C", bg="#FBF9F4", field="#FFFFFF", line="#ECE7DB", cta="#FFFFFF", dark=False),
    "easy":    dict(accent="#16A34A", accentD="#0E7A36", soft="#E7F8EC", muted="#6B7566", ink="#20321F", bg="#FCF9F3", field="#FFFFFF", line="#EEE7D8", cta="#FFFFFF", dark=False),
    "pro":     dict(accent="#C7F94E", accentD="#9FE01F", soft="#1B211E", muted="#8A968F", ink="#F2F5F3", bg="#0A0D0C", field="#141917", line="rgba(255,255,255,.11)", cta="#0A0D0C", dark=True),
    "chef":    dict(accent="#C2683D", accentD="#A2502B", soft="#F0E6D8", muted="#7C7268", ink="#2A2420", bg="#F5F0E6", field="#FFFDF8", line="#E6DDCC", cta="#FFFFFF", dark=False),
    "coach":   dict(accent="#6C4CF1", accentD="#5638DA", soft="#F3F0FF", muted="#6B6786", ink="#181430", bg="#FFFFFF", field="#FFFFFF", line="#ECE9F6", cta="#FFFFFF", dark=False),
    "reset":   dict(accent="#9B8CEB", accentD="#7A69D6", soft="#EFEAFB", muted="#8B86A3", ink="#3A3450", bg="#FAF7FF", field="#FFFFFF", line="#EEE9F8", cta="#FFFFFF", dark=False),
}


# ---------- счётчики (визиты/воронка по лендингу) ----------

def _bump(name: str) -> None:
    if notrack():
        return
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        d = json.loads(COUNTERS.read_text()) if COUNTERS.exists() else {}
        d[name] = int(d.get(name, 0)) + 1
        COUNTERS.write_text(json.dumps(d, ensure_ascii=False))
    except Exception:
        pass


def _counters() -> dict:
    try:
        return json.loads(COUNTERS.read_text()) if COUNTERS.exists() else {}
    except Exception:
        return {}


# ---------- rate-limit (память процесса; для anti-abuse достаточно) ----------
_RATE: dict[str, list] = {}


def _rate_ok(bucket: str, key: str, limit: int, window_sec: int) -> bool:
    """True если действие в пределах лимита. Скользящее окно, in-memory."""
    now = datetime.now(timezone.utc).timestamp()
    k = f"{bucket}:{key}"
    hist = [t for t in _RATE.get(k, []) if now - t < window_sec]
    if len(hist) >= limit:
        _RATE[k] = hist
        return False
    hist.append(now)
    _RATE[k] = hist
    if len(_RATE) > 20000:  # не растим бесконечно
        _RATE.clear()
    return True


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return (xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")) or "?"


# ── Согласие на обработку ПДн ────────────────────────────────────────────────
# Вариант B доктрины golive-legal: не чекбокс, а подпись прямо над кнопкой.
# 152-ФЗ формы не предписывает — ст. 9 требует, чтобы согласие было конкретным,
# информированным и однозначным, и чтобы оператор МОГ ЕГО ПОДТВЕРДИТЬ. Поэтому
# подпись допустима только вместе с полной фиксацией ниже.
#
# Текст версионирован: меняешь формулировку — поднимаешь версию, иначе старые
# записи будут утверждать, что человек согласился с текстом, которого не видел.
CONSENT_VERSION = "v1-2026-07-29"
CONSENT_TEXT = (
    "Нажимая кнопку, вы соглашаетесь с Согласием на обработку персональных "
    "данных и Политикой конфиденциальности."
)
# Тот же текст со ссылками. Держим рядом с CONSENT_TEXT, чтобы правка одного
# без другого сразу бросалась в глаза: в записи о согласии лежит CONSENT_TEXT,
# а человек видит CONSENT_HTML — они обязаны совпадать дословно.
CONSENT_HTML = (
    "Нажимая кнопку, вы соглашаетесь с "
    "<a href='/consent'>Согласием на обработку персональных данных</a> и "
    "<a href='/privacy'>Политикой конфиденциальности</a>."
)
CONSENTS = DATA / "consents.jsonl"


def _record_consent(request: Request, email: str, method: str,
                    text: str = "", version: str = "") -> None:
    """Записать факт согласия так, чтобы он что-то доказывал через год.

    `consent: true` + время не доказывают ничего: московское УФАС однажды не
    приняло галочку как доказательство, потому что нельзя было показать, КТО
    и НА ЧТО согласился. Поэтому девять полей, а не два.

    Текст и версию берём ПРИСЛАННЫЕ клиентом (что реально было на экране), но
    сверяем с текущими: расхождение само по себе — сигнал, что где-то остался
    старый фронт, и его надо видеть, а не молча подменять серверными.
    """
    shown = (text or "").strip() or CONSENT_TEXT
    ver = (version or "").strip() or CONSENT_VERSION
    rec = {
        "at": datetime.now(timezone.utc).isoformat(),
        "email": (email or "").strip().lower(),
        "method": method,                 # button_click | checkbox
        "text": shown,                    # дословно то, что человек видел
        "version": ver,
        "stale_wording": ver != CONSENT_VERSION,
        "ip": _client_ip(request),
        "ua": request.headers.get("user-agent", "")[:400],
        "page": str(request.headers.get("referer") or request.url.path),
        "lang": request.headers.get("accept-language", "")[:120],
    }
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(CONSENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass  # согласие не должно ронять вход


def _secret_ok(param_val: str, request: Request, env_name: str, header_name: str) -> bool:
    """Секрет из query ИЛИ заголовка (заголовок предпочтителен — не течёт в логи/Referer)."""
    import hmac
    want = os.getenv(env_name, "").strip()
    if not want:
        return False
    got = request.headers.get(header_name, "") or param_val or ""
    return hmac.compare_digest(got, want)


# ---------- resume-токены лида: письмо ведёт сразу на пейволл с готовой нормой ----------
LEAD_TOKENS = DATA / "lead_tokens.json"


def _lead_token_make(email: str, quiz: dict) -> str:
    import uuid
    tok = uuid.uuid4().hex
    now = datetime.now(timezone.utc).timestamp()
    try:
        d = json.loads(LEAD_TOKENS.read_text()) if LEAD_TOKENS.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    d = {k: v for k, v in d.items() if v.get("exp", 0) > now}  # чистим протухшие
    d[tok] = {"email": email, "quiz": quiz, "exp": now + 30 * 86400}
    if len(d) > 10000:
        d = dict(sorted(d.items(), key=lambda kv: kv[1].get("exp", 0))[-6000:])
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        LEAD_TOKENS.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return tok


def _lead_token_get(tok: str) -> dict:
    tok = "".join(c for c in (tok or "") if c.isalnum())
    if not tok:
        return {}
    now = datetime.now(timezone.utc).timestamp()
    try:
        d = json.loads(LEAD_TOKENS.read_text()) if LEAD_TOKENS.exists() else {}
        rec = d.get(tok)
        if rec and rec.get("exp", 0) > now:
            return {"email": rec.get("email", ""), "quiz": rec.get("quiz") or {}}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _detect_src(request: Request) -> str:
    q = request.query_params
    if q.get("yclid") or q.get("gclid"):
        return "ad"
    if (q.get("utm_medium") or "").lower() in ("cpc", "ppc", "paid"):
        return "ad"
    if (q.get("utm_source") or "").lower() in ("yandex", "direct"):
        return "ad"
    return "organic"


# ---------- роуты ----------

LANDING_COOKIE = "np_l"
LANDING_COOKIE_AGE = 60 * 60 * 24 * 60  # 60 дней


@app.get("/", response_class=HTMLResponse)
def root(request: Request) -> RedirectResponse:
    # вернувшийся пользователь → его угол (по cookie), иначе дефолт
    slug = request.cookies.get(LANDING_COOKIE, "")
    dest = slug if slug in LANDINGS else DEFAULT_LANDING
    return RedirectResponse(f"/l/{dest}", status_code=302)


NUTRI_METRIKA_ID = os.getenv("NUTRI_METRIKA_ID", "").strip()


def _inject_metrika(html: str) -> str:
    """Вставить счётчик Я.Метрики перед </head>, ЕСЛИ задан NUTRI_METRIKA_ID (инфра готова —
    оператору достаточно задать env, код появится на всех лендингах/квизе/плане автоматически)."""
    if notrack() or not (NUTRI_METRIKA_ID and "</head>" in html):
        return html
    cid = NUTRI_METRIKA_ID
    snippet = (
        "<script type='text/javascript'>(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){"
        "(m[i].a=m[i].a||[]).push(arguments)};m[i].l=1*new Date();"
        "for(var j=0;j<document.scripts.length;j++){if(document.scripts[j].src===r){return;}}"
        "k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)})"
        "(window,document,'script','https://mc.yandex.ru/metrika/tag.js','ym');"
        f"ym({cid},'init',{{clickmap:true,trackLinks:true,accurateTrackBounce:true,webvisor:false}});"
        # Идентификатор наружу: любая страница шлёт цели через window.npGoal(),
        # не зная номера счётчика и не ломаясь, когда счётчик не настроен.
        f"window.NP_METRIKA_ID={cid};"
        "window.npGoal=function(n){try{if(window.ym&&window.NP_METRIKA_ID)"
        "ym(window.NP_METRIKA_ID,'reachGoal',n);}catch(e){}};</script>"
        f"<noscript><div><img src='https://mc.yandex.ru/watch/{cid}' style='position:absolute;left:-9999px' alt='' /></div></noscript>")
    return html.replace("</head>", snippet + "</head>", 1)


@app.get("/l/{slug}", response_class=HTMLResponse)
def landing(slug: str, request: Request, preview: str = "") -> HTMLResponse:
    if slug not in LANDINGS:
        return RedirectResponse(f"/l/{DEFAULT_LANDING}", status_code=302)
    # preview=1 (превью в iframe на /all или в других embed) — НЕ накручиваем счётчики
    # и НЕ ставим куку угла (иначе /all затирал бы np_l и бампал визиты 8 раз).
    is_preview = preview == "1" or request.headers.get("sec-fetch-dest") == "iframe"
    if not is_preview:
        _bump(f"visit_{slug}")
        _bump(f"visit_{slug}_{_detect_src(request)}")
    f = STATIC / "landings" / f"{slug}.html"
    if not f.exists():
        resp: HTMLResponse = HTMLResponse(
            f"<h1>Лендинг «{LANDINGS[slug]}»</h1>"
            f"<p>Слот готов, дизайн — после выбора референсов.</p>"
            f"<p><a href='/quiz?l={slug}'>→ квиз</a></p>")
    else:
        resp = HTMLResponse(_inject_metrika(f.read_text(encoding="utf-8")))
    if not is_preview:  # запоминаем угол — при заходе на голый домен отдадим эту же версию
        resp.set_cookie(LANDING_COOKIE, slug, max_age=LANDING_COOKIE_AGE, samesite="lax", httponly=True)
    return resp


# SVG-плейсхолдер (тарелка), пока фото блюда не сгенерилось — тонкая линия, без эмодзи
_DISH_PH = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' fill='#E7F8EC'/>"
    "<circle cx='32' cy='32' r='18' fill='#fff'/>"
    "<circle cx='32' cy='32' r='18' fill='none' stroke='#BFE3CB' stroke-width='2'/>"
    "<circle cx='32' cy='32' r='9' fill='none' stroke='#BFE3CB' stroke-width='2'/></svg>")


_KNOWN_SLUGS: set[str] | None = None


def _is_known_dish(slug: str) -> bool:
    """Слаг из каталога dishes.json или из любого сохранённого плана — только для таких
    разрешаем платную генерацию фото (защита от амплификации через /dish/произвольное)."""
    global _KNOWN_SLUGS
    if _KNOWN_SLUGS is None:
        s: set[str] = set()
        try:
            import plan_ai
            for d in plan_ai._catalog():
                s.add(dish_photos.slugify(d.get("title", "")))
        except Exception:  # noqa: BLE001
            pass
        _KNOWN_SLUGS = s
    if slug in _KNOWN_SLUGS:
        return True
    # блюдо из свежесгенерированного плана (замены) могло не быть в каталоге — проверим планы
    try:
        for f in PLANS.glob("*.json"):
            pl = json.loads(f.read_text(encoding="utf-8"))
            for d in pl.get("days") or []:
                for m in d.get("meals") or []:
                    if dish_photos.slugify(m.get("name", "")) == slug:
                        _KNOWN_SLUGS.add(slug)
                        return True
    except Exception:  # noqa: BLE001
        pass
    return False


@app.api_route("/dish/{slug}", methods=["GET", "HEAD"])
def dish_photo(slug: str, bg: BackgroundTasks, t: str = "", lg: str = "") -> Response:
    """Фото блюда из общего кэша; если нет — плейсхолдер + фоновая генерация.
    Генерируем ТОЛЬКО для известных блюд (каталог + сохранённые планы) — иначе любой мог бы
    заказывать платную LLM-генерацию произвольных слагов (финансовый DoS + переполнение диска)."""
    slug = "".join(c for c in slug if c.isalnum() or c == "-")[:60]
    if slug and dish_photos.has_photo(slug):
        # lg=1 — просмотр на весь экран: отдаём крупный вариант, если он есть.
        # В списке марка 64px, и тянуть туда крупный файл незачем.
        return FileResponse(str(dish_photos.photo_file(slug, big=lg == "1")), media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=2592000, immutable"})
    if slug and _is_known_dish(slug):
        bg.add_task(dish_photos.generate, slug, t or slug.replace("-", " "))
    return Response(_DISH_PH, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def _legal(name: str) -> HTMLResponse:
    f = STATIC / "legal" / f"{name}.html"
    if f.exists():
        return HTMLResponse(f.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Документ не найден</h1>", status_code=404)


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page() -> HTMLResponse:
    return _legal("privacy")


@app.get("/consent", response_class=HTMLResponse)
def consent_page() -> HTMLResponse:
    return _legal("consent")


@app.get("/offer", response_class=HTMLResponse)
def offer_page() -> HTMLResponse:
    return _legal("offer")


@app.get("/all", response_class=HTMLResponse)
def hub() -> HTMLResponse:
    """Навигационный хаб: живые превью всех лендингов + переходы."""
    cards = ""
    for slug, name in LANDINGS.items():
        title, _, angle = name.partition(" — ")
        cards += f"""
      <a class="card" href="/l/{slug}">
        <div class="preview"><iframe src="/l/{slug}?preview=1" scrolling="no" tabindex="-1" loading="lazy"></iframe><span class="veil"></span></div>
        <div class="body">
          <div class="row"><span class="slug">/l/{slug}</span><span class="open">Открыть &rarr;</span></div>
          <h3>{title}</h3>
          <p>{angle or name}</p>
        </div>
      </a>"""
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NutriPlan — все лендинги</title>
<style>
  :root{{--bg:#FBF9F4;--ink:#16241C;--muted:#5F6F64;--line:#ECE7DB;--green:#16A34A;--green-d:#0F7A37;--lime:#DCFCE7}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}}
  a{{color:inherit;text-decoration:none}}
  header{{position:sticky;top:0;z-index:10;background:rgba(251,249,244,.88);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}}
  .nav{{max-width:1080px;margin:0 auto;padding:0 24px;height:64px;display:flex;align-items:center;justify-content:space-between}}
  .logo{{font-weight:800;font-size:20px;display:flex;align-items:center;gap:8px}}
  .logo .dot{{width:12px;height:12px;border-radius:50%;background:var(--green)}}
  .badge{{background:var(--lime);color:var(--green-d);font-weight:700;font-size:13px;padding:6px 13px;border-radius:99px}}
  main{{max-width:1080px;margin:0 auto;padding:44px 24px 70px}}
  h1{{font-size:clamp(28px,4vw,40px);font-weight:800;letter-spacing:-.02em}}
  .lead{{color:var(--muted);font-size:17px;margin:10px 0 34px;max-width:60ch}}
  .grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:26px}}
  .card{{background:#fff;border:1px solid var(--line);border-radius:22px;overflow:hidden;transition:.2s;display:block}}
  .card:hover{{transform:translateY(-4px);box-shadow:0 24px 50px -24px rgba(0,0,0,.22)}}
  .preview{{position:relative;height:300px;overflow:hidden;background:#F3FBF5;border-bottom:1px solid var(--line)}}
  .preview iframe{{width:1280px;height:1580px;border:0;transform:scale(.36);transform-origin:top left;pointer-events:none}}
  .preview .veil{{position:absolute;inset:0}}
  .body{{padding:20px 22px 24px}}
  .row{{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}}
  .slug{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:var(--muted);background:#F4F1E8;padding:3px 9px;border-radius:7px}}
  .open{{color:var(--green-d);font-weight:700;font-size:14px}}
  .card h3{{font-size:21px;font-weight:800;letter-spacing:-.01em;margin-bottom:5px}}
  .card p{{color:var(--muted);font-size:15px;line-height:1.45}}
  @media(max-width:760px){{.grid{{grid-template-columns:1fr}}.preview{{height:340px}}.preview iframe{{transform:scale(.44)}}}}
</style></head>
<body>
  <header><div class="nav">
    <div class="logo"><span class="dot"></span>NutriPlan</div>
    <span class="badge">Все лендинги</span>
  </div></header>
  <main>
    <h1>Лендинги NutriPlan</h1>
    <p class="lead">{len(LANDINGS)} угла позиционирования — один продукт, разный трафик. Открой любой, чтобы посмотреть целиком.</p>
    <div class="grid">{cards}
    </div>
  </main>
</body></html>"""
    return HTMLResponse(html)


@app.get("/showcase", response_class=HTMLResponse)
def showcase() -> HTMLResponse:
    """Витрина: варианты модуля «приложение» (ряд телефонов) — с маскотом и без."""
    def P(img):
        return f'<div class="phone"><div class="screen"><img src="/assets/{img}" alt=""></div></div>'
    scr = ["app_onboarding.png", "app_today.png", "app_menu.png", "app_progress.png", "app_achievement.png"]
    masc = ('<div class="mascot"><video autoplay loop muted playsinline>'
            '<source src="/assets/avocado_panel.mp4" type="video/mp4"></video></div>')
    html = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>NutriPlan — витрина модуля</title>
<style>
  :root{{--bg:#FBF9F4;--ink:#16241C;--muted:#5F6F64;--line:#ECE7DB;--green:#16A34A;--green-d:#0F7A37;--lime:#DCFCE7}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:1160px;margin:0 auto;padding:0 22px}}
  header{{border-bottom:1px solid var(--line);background:#fff}}
  .nav{{display:flex;align-items:center;justify-content:space-between;height:64px}}
  .logo{{font-weight:800;font-size:20px;display:flex;align-items:center;gap:8px}}
  .logo .dot{{width:12px;height:12px;border-radius:50%;background:var(--green)}}
  h1{{font-size:30px;font-weight:800;letter-spacing:-.02em;margin:38px 0 6px}}
  .lead{{color:var(--muted);font-size:16px;margin-bottom:8px}}
  .variant{{padding:56px 0;border-bottom:1px solid var(--line)}}
  .tag{{display:inline-block;background:var(--lime);color:var(--green-d);font-weight:800;font-size:12px;letter-spacing:.04em;
    text-transform:uppercase;padding:6px 13px;border-radius:99px}}
  .variant h2{{font-size:23px;font-weight:800;letter-spacing:-.01em;margin:12px 0 4px}}
  .variant .d{{color:var(--muted);font-size:15px;margin-bottom:34px}}
  .stage{{overflow:hidden}}
  /* phone base */
  .phone{{width:172px;flex:0 0 auto;background:#101822;border-radius:30px;padding:7px;box-shadow:0 40px 74px -34px rgba(16,60,40,.45)}}
  .screen{{background:#fff;border-radius:24px;overflow:hidden;aspect-ratio:704/1520}}
  .screen>img{{width:100%;height:100%;object-fit:cover;display:block}}
  .mascot{{border-radius:26px;overflow:hidden;background:#A3CFB9;box-shadow:0 26px 46px -22px rgba(20,80,45,.45)}}
  .mascot video{{width:100%;display:block}}
  .bubble{{background:#fff;border:1px solid var(--line);border-radius:16px 16px 16px 4px;padding:10px 14px;font-size:14px;
    font-weight:600;box-shadow:0 16px 30px -16px rgba(0,0,0,.2);max-width:190px}}
  .bubble b{{color:var(--green-d)}}
  /* A — arc */
  .va{{display:flex;justify-content:center;align-items:flex-end;gap:8px}}
  .va .phone:nth-child(1){{transform:translateY(30px) rotate(-6deg)}}
  .va .phone:nth-child(2){{transform:translateY(12px) rotate(-3deg)}}
  .va .phone:nth-child(3){{transform:translateY(0);z-index:2}}
  .va .phone:nth-child(4){{transform:translateY(12px) rotate(3deg)}}
  .va .phone:nth-child(5){{transform:translateY(30px) rotate(6deg)}}
  /* B — feature split */
  .vb{{display:grid;grid-template-columns:280px 1fr;gap:52px;align-items:center;max-width:900px;margin:0 auto}}
  .vb .phone{{width:280px;transform:rotate(-2deg)}}
  .vb .feats{{display:flex;flex-direction:column;gap:22px}}
  .vb .feat{{display:flex;gap:15px;align-items:flex-start}}
  .vb .feat .ic{{width:46px;height:46px;border-radius:13px;background:var(--lime);flex:0 0 auto;display:flex;align-items:center;justify-content:center}}
  .vb .feat .ic svg{{width:23px;height:23px}}
  .vb .feat h4{{font-size:17px;margin-bottom:3px}}.vb .feat p{{color:var(--muted);font-size:14px}}
  /* C — mascot beside (guide) */
  .vc{{display:flex;justify-content:center;align-items:flex-end;gap:26px}}
  .vc .guide{{display:flex;flex-direction:column;align-items:center;gap:14px;align-self:flex-end}}
  .vc .mascot{{width:150px}}
  .vc .phones{{display:flex;align-items:flex-end;gap:8px}}
  .vc .phones .phone{{width:152px}}
  /* D — mascot in the gap + bubble */
  .vd{{display:flex;justify-content:center;align-items:flex-end;gap:10px}}
  .vd .phone{{width:150px}}
  .vd .slot{{display:flex;flex-direction:column;align-items:center;gap:12px;align-self:flex-end}}
  .vd .slot .mascot{{width:120px}}
  @media(max-width:900px){{
    .va,.vc .phones,.vd{{overflow-x:auto;justify-content:flex-start;padding:8px 4px}}
    .vb{{grid-template-columns:1fr;gap:26px;justify-items:center;text-align:left}}
    .vc{{flex-direction:column;align-items:center;gap:18px}}
  }}
</style></head><body>
  <header><div class="wrap nav"><div class="logo"><span class="dot"></span>NutriPlan</div><span class="tag">Витрина модуля</span></div></header>
  <div class="wrap">
    <h1>Модуль «Приложение» — варианты</h1>
    <p class="lead">Один и тот же ряд экранов, 4 разных подачи. Выбери — раскатаю на все лендинги (slim / energy / easy / coach).</p>
  </div>

  <section class="variant"><div class="wrap">
    <span class="tag">Вариант A · без маскота</span>
    <h2>Дуга (fan)</h2>
    <p class="d">Экраны веером: центральный крупнее, крайние ниже и повёрнуты. Чисто, премиально, всё внимание на приложение.</p>
    <div class="stage"><div class="va">{''.join(P(s) for s in scr)}</div></div>
  </div></section>

  <section class="variant"><div class="wrap">
    <span class="tag">Вариант B · без маскота</span>
    <h2>Фича-сплит</h2>
    <p class="d">Один крупный экран + список возможностей рядом. Не просто «красиво», а объясняет ценность — лучше конвертит.</p>
    <div class="vb">
      {P("app_today.png")}
      <div class="feats">
        <div class="feat"><div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="#0F7A37" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8 4h8a2 2 0 0 1 2 2v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V6a2 2 0 0 1 2-2z"/><path d="M9 10h6M9 14h4"/></svg></div><div><h4>Меню на неделю</h4><p>Готовый рацион под твои вкусы и цель</p></div></div>
        <div class="feat"><div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="#0F7A37" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20a8 8 0 1 0-8-8"/><path d="M12 12l4-2"/></svg></div><div><h4>КБЖУ автоматом</h4><p>Всё посчитано — не держишь в голове</p></div></div>
        <div class="feat"><div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="#0F7A37" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="20" r="1.3"/><circle cx="17" cy="20" r="1.3"/><path d="M3 4h2l2.2 11h9.5l1.5-7.5H6.2"/></svg></div><div><h4>Список покупок</h4><p>Собран сам, по разделам магазина</p></div></div>
        <div class="feat"><div class="ic"><svg viewBox="0 0 24 24" fill="none" stroke="#0F7A37" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-5 4 3 8-8"/><path d="M21 7v4h-4"/></svg></div><div><h4>Прогресс и стрики</h4><p>Видишь движение к цели каждый день</p></div></div>
      </div>
    </div>
  </div></section>

  <section class="variant"><div class="wrap">
    <span class="tag">Вариант C · с маскотом</span>
    <h2>Маскот-гид сбоку</h2>
    <p class="d">Авокадо стоит рядом с рядом экранов на одной линии и «представляет» приложение. Не наезжает ни на текст, ни на экраны.</p>
    <div class="stage"><div class="vc">
      <div class="guide"><div class="bubble">Я рядом на каждом шаге <b>— подскажу и поддержу</b></div>{masc}</div>
      <div class="phones">{''.join(P(s) for s in scr[:4])}</div>
    </div></div>
  </div></section>

  <section class="variant"><div class="wrap">
    <span class="tag">Вариант D · с маскотом</span>
    <h2>Маскот в разрыве + реплика</h2>
    <p class="d">Авокадо стоит в разрыве ряда на общей нижней линии (он ниже экранов) — вписан как часть композиции, а не парит поверх.</p>
    <div class="stage"><div class="vd">
      {P(scr[0])}{P(scr[1])}
      <div class="slot"><div class="bubble">Серия 5 дней — <b>так держать!</b></div>{masc}</div>
      {P(scr[3])}{P(scr[4])}
    </div></div>
  </div></section>

  <div class="wrap" style="padding:40px 22px 70px;color:var(--muted)">Скажи номер варианта (A / B / C / D) — раскатаю на лендинги. Можно и комбинировать (напр. B на «серьёзных», C на coach).</div>
  <script>[...document.querySelectorAll('video')].forEach(v=>{{v.muted=true;v.play().catch(()=>{{}})}});</script>
</body></html>"""
    return HTMLResponse(html)


@app.get("/quiz", response_class=HTMLResponse)
def quiz(request: Request, l: str = DEFAULT_LANDING) -> HTMLResponse:
    """Единый квиз, темизированный под лендинг (?l=slug)."""
    slug = l if l in LANDINGS else DEFAULT_LANDING
    _bump(f"quiz_{slug}")
    t = THEME.get(slug, THEME[DEFAULT_LANDING])
    html = (STATIC / "quiz.html").read_text(encoding="utf-8")
    theme_css = (
        ":root{"
        f"--accent:{t['accent']};--accent-d:{t['accentD']};--soft:{t['soft']};"
        f"--muted:{t['muted']};--ink:{t['ink']};--bg:{t['bg']};--field:{t['field']};"
        f"--line:{t['line']};--cta-ink:{t['cta']};"
        "}"
    )
    html = (html.replace("/*THEMEHOOK*/", theme_css)
                .replace("__LANDING__", slug)
                .replace("__SRC__", _detect_src(request))
                .replace("__DARK__", "1" if t["dark"] else "0")
                .replace("__PRICE__", str(int(PRICE_RUB)))
                .replace("__SUB_PRICE__", str(int(SUB_PRICE_RUB))))
    return HTMLResponse(_inject_metrika(html))


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True})


class Lead(BaseModel):
    email: EmailStr
    landing: str = ""
    goal: str = ""
    quiz: dict = {}
    src: str = "organic"


# Блюда для «Примера дня» на экране оплаты.
#
# Каталог dishes.json — это общая домашняя кухня: там есть и пицца пепперони, и
# картофельное пюре с сосисками, и шашлык. Показывать их человеку, который две
# минуты назад ответил «хочу похудеть», нельзя — экран сразу перестаёт выглядеть
# как план питания. Калорийности в каталоге нет, отобрать «по весу» нечем,
# поэтому набор отобран руками.
#
# Все названия ВЗЯТЫ ИЗ КАТАЛОГА, а не выдуманы: только для каталожных блюд
# существуют (и до-генерируются) фото, и только они попадают в оплаченный план.
# Отобранные блюда для «Примера дня» + короткий состав к каждому.
#
# ПРАВИЛО ОТБОРА: в каждом приёме пищи есть источник белка, и блюдо
# правдоподобно набирает свою долю калорий (30/40/30 — это примерно
# 480 / 640 / 480 при норме 1600).
#
# Предыдущий набор отбирался по признаку «выглядит диетично» — овощное, лёгкое.
# На экране это давало винегрет на 600 ккал и фруктовый салат на 450: тарелка
# овощей столько не весит, а белка в ней нет вовсе. И это спорило с двумя
# надписями на том же экране — «норма без голода» и «103 г белки».
#
# Порядок значим: берём ПЕРВОЕ подходящее блюдо, поэтому во главе списка стоит
# то, что мы хотим показывать по умолчанию. Человек без ограничений увидит
# омлет и запечённую рыбу — так задумано; разнообразие даёт не случайность, а
# фильтр по его же ответам.
#
# Состав описывает САМО БЛЮДО, а не наш рецепт: омлет с овощами состоит из яиц,
# помидоров и зелени независимо от того, кто его готовит. Граммовок нет
# намеренно — рецепт в оплаченном плане собирает модель.
PREVIEW_PICKS = {
    "breakfast": {
        "Омлет с овощами": "Яйца, помидоры, зелень",
        "Яичница с помидорами": "Яйца, помидоры, зелень",
        "Творог с ягодами": "Творог и свежие ягоды",
        "Творожная запеканка": "Творог, яйца, изюм",
        "Омлет с сыром": "Яйца, сыр, немного масла",
        "Сырники со сметаной": "Творог, яйцо, мука, сметана",
        "Йогурт с гранолой": "Йогурт, гранола, фрукты",
        "Сэндвич с курицей": "Хлеб, курица, овощи",
        "Овсяная каша с фруктами": "Овсянка, фрукты, мёд",
        # Ниже — «без мяса + без лактозы». Белковых завтраков каталог для этой
        # комбинации не содержит вовсе (проверено: только тосты, овсянка на
        # воде и фруктовый салат), поэтому берём хотя бы сытные по жиру.
        "Тосты с авокадо": "Хлеб, авокадо, лимон",
        "Овсянка на воде": "Овсянка, корица, фрукты",
        # Самый последний резерв — «без мяса + без лактозы + без глютена».
        # Для этой комбинации в каталоге есть РОВНО ОДНО блюдо на завтрак, и это
        # оно. Белка в нём нет, для плана похудения это плохой завтрак — но
        # альтернатива хуже: провалиться в запасную ветку и показать блюдо без
        # состава. Настоящее решение — дополнить каталог веганским белковым
        # завтраком (тофу, киноа, растительное молоко), этого там нет вовсе.
        "Фруктовый салат": "Свежие фрукты и ягоды",
    },
    "lunch": {
        "Рыба запеченная с овощами": "Белая рыба и овощи",
        "Куриная грудка с рисом": "Курица, рис, овощи",
        "Гречка с котлетой": "Гречка, котлета, овощи",
        "Тефтели с рисом": "Тефтели, рис, подлива",
        "Плов с курицей": "Рис, курица, морковь, лук",
        "Цезарь с курицей": "Курица, романо, пармезан",
        "Куриные отбивные": "Курица, гарнир, овощи",
        # Вегетарианцу каталог даёт мало белковых обедов — чечевица здесь
        # единственный настоящий вариант, остальное картошка и овощные супы.
        "Чечевичный суп": "Чечевица, морковь, лук",
        "Картофельная запеканка": "Картофель, сыр, сливки",
    },
    "dinner": {
        "Куриное филе с овощами": "Куриное филе и брокколи",
        "Рыбное филе запеченное": "Белая рыба, лимон, травы",
        "Стейк из лосося": "Лосось, лимон, травы",
        "Куриная запеканка": "Курица, овощи, сыр",
        "Рыбная запеканка": "Рыба, картофель, сливки",
        "Куриные рулеты": "Курица, сыр, зелень",
        "Запеканка из творога": "Творог, яйца, изюм",
        "Фалафель в пите": "Нут, пита, овощи, соус",
        "Овощной салат с фетой": "Огурцы, помидоры, фета",
        # Резерв для «без мяса + без лактозы + без глютена». Порядок по белку:
        # яйца, потом нут, потом просто овощи с крупой.
        "Яичница с грибами": "Яйца, шампиньоны, зелень",
        "Хумус с овощами": "Нут, тахини, свежие овощи",
        "Вегетарианский плов": "Рис, морковь, лук, специи",
    },
}


# Исключения из свободного текста для превью.
#
# plan_ai сверяет их только с НАЗВАНИЕМ блюда, и этого мало: человек пишет
# «яйца» — а «Омлет с овощами» в названии яиц не содержит и остаётся на экране.
# Пишет «рыба» — уезжает «Рыба запеченная», но остаётся «Стейк из лосося».
#
# В превью у нас есть состав каждого блюда, который мы сами и написали, поэтому
# сверяем по «название + состав» и раскрываем зонтичные слова в конкретные.
# Это не заменяет фильтр каталога, а достраивает его на девяти блюдах, которые
# реально попадают на экран покупки.
PREVIEW_SYNONYMS = {
    "рыб": ["рыб", "лосось", "лосос", "тунец", "сельдь", "минтай", "треск", "форель", "скумбри", "морепродукт", "креветк"],
    "мяс": ["мяс", "котлет", "тефтел", "говядин", "свинин", "фарш", "бекон", "колбас", "ветчин", "курин", "куриц", "индейк"],
    "куриц": ["куриц", "курин", "курк"],
    "курин": ["куриц", "курин"],
    "яйц": ["яйц", "яич", "омлет"],
    "молок": ["молок", "молоч", "сливк", "сметан", "творог", "сыр", "йогурт", "фет", "пармезан"],
    "молоч": ["молок", "молоч", "сливк", "сметан", "творог", "сыр", "йогурт", "фет", "пармезан"],
    "сыр": ["сыр", "фет", "пармезан"],
    "творог": ["творог", "сырник"],
    "греч": ["гречк", "гречнев"],
}


PREVIEW_UMBRELLA = {          # слово → флаг каталога, который надо отсечь целиком
    "молочн": "lact", "молок": "lact", "лактоз": "lact",
    "рыб": "fish",
    "мяс": "meat",
}


def _preview_flags(quiz: dict) -> set:
    """Какие категории каталога человек исключил зонтичным словом."""
    raw = str(quiz.get("exclude") or "").lower()
    out = set()
    for word, flag in PREVIEW_UMBRELLA.items():
        if word in raw:
            out.add(flag)
    return out


def _preview_excluded(quiz: dict) -> list[str]:
    """Стемы того, что человек написал в «что ещё не ешь», с раскрытием
    зонтичных слов: «рыба» должна убирать и лосося тоже."""
    raw = str(quiz.get("exclude") or "")
    terms: list[str] = []
    for piece in raw.replace(";", ",").split(","):
        t = piece.strip().lower()
        if len(t) < 3:
            continue
        # Ищем зонтичное слово по ПРЕФИКСУ в обе стороны. Обрезать хвост по
        # длине не годится: «рыба» давала стем «рыба», ключа «рыб» не находила,
        # и лосось оставался на экране у того, кто не ест рыбу.
        hit = None
        for key, syns in PREVIEW_SYNONYMS.items():
            if t.startswith(key) or key.startswith(t):
                hit = syns
                break
        terms.extend(hit if hit else [t[:-1] if len(t) > 4 else t])
    return terms


# Чего в превью не показываем НИКОГДА, даже из запасной ветки: она берёт весь
# разрешённый каталог, а там пицца, бургеры и шашлык. На экране плана похудения
# это читается как насмешка над только что заданной целью.
PREVIEW_DENY = {
    "Пицца Пепперони", "Пицца Маргарита", "Пицца Четыре сыра",
    "Бургер с говядиной", "Бургер с курицей", "Шашлык из свинины", "Шашлык из курицы",
    "Жареная картошка с грибами", "Картошка по-деревенски", "Картофельное пюре с сосисками",
    "Блинчики с мясом", "Панкейки с сиропом", "Круассан с джемом", "Оладьи с вареньем",
    "Макароны по-флотски", "Макароны с сыром", "Вареники с вишней",
}


class PreviewReq(BaseModel):
    quiz: dict = {}


@app.post("/api/preview/day")
def preview_day(req: PreviewReq) -> JSONResponse:
    """Первый день плана по ответам квиза — для экрана оплаты.

    Раньше «Пример дня» был захардкожен в вёрстке: всем показывались овсянка,
    курица и рыба — в том числе тем, кто ответил «без мяса», прямо под фразой
    «собран под твои вкусы».

    Берём блюда из ТОГО ЖЕ каталога, из которого собирается ОПЛАЧЕННЫЙ план
    (dishes.json через plan_ai._allowed_by_meal): он учитывает вегетарианство,
    рыбу, лактозу, аллергены и исключения — и для его блюд гарантированно есть
    фото. Старый банк в plan.py на это не способен: он ветвится только по
    «без мяса», а его 36 блюд вообще отсутствуют в каталоге, поэтому картинок
    для них не будет никогда (генерация намеренно ограничена каталогом).

    Выбор детерминированный: один и тот же человек при обновлении страницы
    видит тот же день, разные — разные блюда.

    Побочных эффектов нет: ничего не пишем, почту не принимаем, счётчики не
    трогаем.
    """
    import plan
    quiz = req.quiz or {}
    kc = plan.compute(quiz)["cal"]
    split = [round(kc * 0.30), round(kc * 0.40), round(kc * 0.30)]
    labels = [("Завтрак", "breakfast"), ("Обед", "lunch"), ("Ужин", "dinner")]

    meals = []
    excl_terms = _preview_excluded(quiz)
    excl_flags = _preview_flags(quiz)
    try:
        import plan_ai
        allowed = plan_ai._allowed_by_meal(quiz)
        by_title = {d["title"]: d for d in plan_ai._catalog()}
        for i, (label, key) in enumerate(labels):
            pool = [t for t in (allowed.get(key) or []) if t not in PREVIEW_DENY]
            # Отсекаем то, что человек написал руками, — по названию И составу.
            if excl_terms:
                picks_map = PREVIEW_PICKS.get(key, {})
                pool = [t for t in pool
                        if not any(x in (t + " " + picks_map.get(t, "")).lower()
                                   for x in excl_terms)]
            if excl_flags:
                def _ok(t: str) -> bool:
                    d = by_title.get(t, {})
                    if "lact" in excl_flags and d.get("lact"):
                        return False
                    if "fish" in excl_flags and d.get("fish"):
                        return False
                    if "meat" in excl_flags and not d.get("veg") and not d.get("fish"):
                        return False
                    return True
                pool = [t for t in pool if _ok(t)]
            # Сначала отобранные, и только если ограничения вырезали их все —
            # весь разрешённый каталог: лучше неожиданное блюдо, чем пустой
            # экран у человека с редким набором ограничений.
            picks = [t for t in PREVIEW_PICKS.get(key, {}) if t in pool]
            pool = picks or pool
            if not pool:
                meals = []
                break
            title = pool[0]
            d = by_title.get(title, {})
            meals.append({"meal": label, "title": title, "kcal": split[i],
                          "desc": PREVIEW_PICKS.get(key, {}).get(title, ""),
                          "slug": d.get("slug", ""),
                          "photo": bool(d.get("slug")) and dish_photos.has_photo(d["slug"])})
    except Exception:  # noqa: BLE001
        meals = []

    if not meals:
        # Каталог не дал ни одного блюда под эти ограничения — отдаём запасной
        # день из банка. Он беднее, зато существует всегда.
        try:
            day = plan.week(quiz)[0]
            meals = [{"meal": name, "title": title, "kcal": kcal, "desc": "",
                      "slug": dish_photos.slugify(title),
                      "photo": dish_photos.has_photo(dish_photos.slugify(title))}
                     for name, title, kcal in day["meals"]]
        except Exception:  # noqa: BLE001
            return JSONResponse({"meals": []})

    return JSONResponse({"meals": meals})


@app.post("/api/lead")
def save_lead(lead: Lead, request: Request, bg: BackgroundTasks) -> JSONResponse:
    rec = lead.model_dump()
    rec["ts"] = datetime.now(timezone.utc).isoformat()
    slug = lead.landing if lead.landing in LANDINGS else "?"
    _bump(f"lead_{slug}")
    if notrack():
        # Свой прогон: ни строки в лидах, ни письма. Отвечаем как обычно —
        # фронт должен вести себя ровно так же, иначе тестируется не то.
        return JSONResponse({"ok": True})
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LEADS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # квиз-лид → письмо с готовой нормой + ссылкой СРАЗУ на пейволл (не на старт квиза заново)
    if lead.quiz and lead.email:
        base = str(request.base_url).rstrip("/")
        ls = lead.landing if lead.landing in LANDINGS else DEFAULT_LANDING
        tok = _lead_token_make(str(lead.email), lead.quiz)
        link = f"{base}/quiz?l={ls}&resume={tok}"  # resume → квиз восстановит норму и покажет пейволл
        bg.add_task(_send_lead, str(lead.email), lead.quiz, link)
    return JSONResponse({"ok": True})


@app.get("/api/lead/resume/{token}")
def lead_resume(token: str) -> JSONResponse:
    """Данные лида по resume-токену (для восстановления пейволла из письма)."""
    rec = _lead_token_get(token)
    if not rec:
        return JSONResponse({"error": "expired"}, status_code=404)
    return JSONResponse({"email": rec["email"], "quiz": rec["quiz"]})


def _write_order(rec: dict) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(ORDERS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


class PayReq(BaseModel):
    email: EmailStr
    landing: str = ""
    goal: str = ""
    quiz: dict = {}
    src: str = "organic"


@app.post("/api/pay/create")
def pay_create(req: PayReq, request: Request) -> JSONResponse:
    """Создать платёж в ЮKassa → вернуть URL страницы оплаты."""
    if not (YOOKASSA_SHOP and YOOKASSA_SECRET):
        return JSONResponse({"error": "payments not configured"}, status_code=503)
    import uuid
    import requests
    slug = req.landing if req.landing in LANDINGS else DEFAULT_LANDING
    oid = uuid.uuid4().hex
    base = str(request.base_url).rstrip("/")
    val = f"{float(PRICE_RUB):.2f}"
    body = {
        "amount": {"value": val, "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": f"{base}/pay/success?o={oid}"},
        "description": f"NutriPlan — персональный план питания ({slug})",
        "metadata": {"order": oid, "email": req.email, "landing": slug},
        "receipt": {  # 54-ФЗ: чек на почту клиента
            "customer": {"email": req.email},
            "items": [{
                "description": "Персональный план питания на 7 дней",
                "quantity": "1.00",
                "amount": {"value": val, "currency": "RUB"},
                "vat_code": 1, "payment_subject": "service", "payment_mode": "full_payment",
            }],
        },
    }
    try:
        r = requests.post("https://api.yookassa.ru/v3/payments",
                          auth=(YOOKASSA_SHOP, YOOKASSA_SECRET),
                          headers={"Idempotence-Key": oid, "Content-Type": "application/json"},
                          json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
        url = (j.get("confirmation") or {}).get("confirmation_url", "")
        # pay_url сохраняем, чтобы человек мог ВЕРНУТЬСЯ к неоплаченному платежу.
        # Без него единственным выходом с экрана «платёж обрабатывается» было
        # оформить заказ заново.
        _write_order({"order": oid, "payment_id": j.get("id"), "status": j.get("status"),
                      "email": req.email, "landing": slug, "goal": req.goal, "quiz": req.quiz,
                      "src": req.src, "amount": PRICE_RUB, "pay_url": url,
                      "ts": datetime.now(timezone.utc).isoformat()})
        _bump(f"pay_init_{slug}")
        return JSONResponse({"url": url}) if url else JSONResponse({"error": "no url"}, status_code=502)
    except Exception:
        return JSONResponse({"error": "yookassa error"}, status_code=502)


@app.post("/api/pay/subscribe")
def pay_subscribe(req: PayReq, request: Request) -> JSONResponse:
    """Первый платёж подписки — с save_payment_method (сохранить способ для автосписаний)."""
    if not (YOOKASSA_SHOP and YOOKASSA_SECRET):
        return JSONResponse({"error": "payments not configured"}, status_code=503)
    import uuid
    import requests
    # Уже есть активная подписка на этот email → не создаём вторую (иначе вторая невидима
    # в ЛК и списывает деньги после «отмены» первой). Отдаём ссылку на существующий план.
    _em = str(req.email).strip().lower()
    for s in _all_subs():
        if s.get("email", "").strip().lower() == _em and s.get("status") == "active" \
                and s.get("payment_method_id"):
            base = str(request.base_url).rstrip("/")
            return JSONResponse({"already": True, "plan_url": f"{base}/plan/{s.get('plan_token','')}"})
    slug = req.landing if req.landing in LANDINGS else DEFAULT_LANDING
    sid = uuid.uuid4().hex
    base = str(request.base_url).rstrip("/")
    val = f"{float(SUB_PRICE_RUB):.2f}"
    body = {
        "amount": {"value": val, "currency": "RUB"}, "capture": True,
        "save_payment_method": True,
        "confirmation": {"type": "redirect", "return_url": f"{base}/pay/success?o={sid}"},
        "description": f"NutriPlan — подписка ({slug})",
        "metadata": {"type": "subscription", "order": sid, "email": req.email, "landing": slug},
        "receipt": {"customer": {"email": req.email}, "items": [{
            "description": "Подписка NutriPlan (1 месяц)", "quantity": "1.00",
            "amount": {"value": val, "currency": "RUB"},
            "vat_code": 1, "payment_subject": "service", "payment_mode": "full_payment"}]},
    }
    try:
        r = requests.post("https://api.yookassa.ru/v3/payments", auth=(YOOKASSA_SHOP, YOOKASSA_SECRET),
                          headers={"Idempotence-Key": sid, "Content-Type": "application/json"},
                          json=body, timeout=30)
        r.raise_for_status()
        j = r.json()
        url = (j.get("confirmation") or {}).get("confirmation_url", "")
        _write_order({"order": sid, "type": "subscription", "payment_id": j.get("id"), "status": j.get("status"),
                      "email": req.email, "landing": slug, "goal": req.goal, "quiz": req.quiz,
                      "src": req.src, "amount": SUB_PRICE_RUB, "pay_url": url,
                      "ts": datetime.now(timezone.utc).isoformat()})
        _bump(f"sub_init_{slug}")
        return JSONResponse({"url": url}) if url else JSONResponse({"error": "no url"}, status_code=502)
    except Exception:
        return JSONResponse({"error": "yookassa error"}, status_code=502)


def _yk_get_payment(pid: str) -> dict:
    """Перепроверка платежа через API ЮKassa — НЕ доверяем телу вебхука (защита от подделки)."""
    if not (YOOKASSA_SHOP and YOOKASSA_SECRET and pid):
        return {}
    try:
        import requests
        r = requests.get(f"https://api.yookassa.ru/v3/payments/{pid}",
                         auth=(YOOKASSA_SHOP, YOOKASSA_SECRET), timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:  # noqa: BLE001
        pass
    return {}


@app.post("/api/pay/webhook")
async def pay_webhook(request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Уведомление ЮKassa (payment.succeeded). URL зарегистрировать в ЛК ЮKassa.

    Тело уведомления НЕ доверенное — платёж перепроверяется через API по id + сверяется сумма."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    obj = payload.get("object") or {}
    if payload.get("event") == "payment.succeeded":
        verified = _yk_get_payment(obj.get("id", ""))
        if not (verified.get("status") == "succeeded" and verified.get("paid")):
            return JSONResponse({"ok": True, "verified": False})  # подделка/неоплачено — игнор
        obj = verified  # дальше используем ТОЛЬКО проверенные данные ЮKassa
        # Идемпотентность: повторная доставка того же payment_id → выходим сразу
        # (иначе сброс next_charge, второе письмо, дубль LLM, воскрешение отмены).
        if _already_processed(obj.get("id", "")):
            return JSONResponse({"ok": True, "duplicate": True})
        meta = obj.get("metadata") or {}
        typ = meta.get("type", "once")
        slug = meta.get("landing", "?")
        oid = meta.get("order", "")
        email = meta.get("email", "")
        order = _find_order(oid)
        # Сверяем оплаченную сумму с суммой ИЗ ЗАКАЗА (env мог смениться после создания
        # заказа — тогда сверка с env ложно завернула бы легитимную оплату). Fallback — env.
        expected = float(order.get("amount") or (SUB_PRICE_RUB if typ in ("subscription", "sub_renew") else PRICE_RUB))
        try:
            paid_val = float((obj.get("amount") or {}).get("value") or 0)
        except Exception:  # noqa: BLE001
            paid_val = 0.0
        if paid_val + 0.01 < expected:
            return JSONResponse({"ok": True, "verified": False, "reason": "amount"})  # недоплата
        base = str(request.base_url).rstrip("/")
        now = datetime.now(timezone.utc)
        _write_order({"order": oid, "type": typ, "payment_id": obj.get("id"), "status": "succeeded",
                      "email": email, "landing": slug, "ts": now.isoformat(), "event": "payment.succeeded"})
        quiz = order.get("quiz") or {}
        if typ == "subscription":
            pm = (obj.get("payment_method") or {}).get("id", "")
            # Если sub уже есть (гонка/повтор) — не сбрасываем счётчики, только гарантируем карту.
            if _load_sub(oid):
                _sub_merge(oid, {"payment_method_id": pm, "status": "active"})
            else:
                _save_sub({"sub_id": oid, "email": email, "landing": slug, "quiz": quiz,
                           "payment_method_id": pm, "amount": SUB_PRICE_RUB, "status": "active",
                           "created": now.isoformat(), "plan_token": oid,
                           "next_charge": (now + timedelta(days=30)).isoformat(),
                           "next_plan": (now + timedelta(days=7)).isoformat()})
            _bump(f"sub_ok_{slug}")
            bg.add_task(_fulfill_paid, email, quiz, oid, base)  # даже при пустом quiz → bank-план + письмо
        elif typ == "sub_renew":
            _bump(f"sub_renew_ok_{slug}")
            # Продление подтверждено. Чистим pending, сдвигаем next_charge (guard в _advance_charge
            # не даст сдвинуть дважды), реанимируем past_due — всё через merge (не затираем cancel).
            s = _load_sub(oid)
            if s:
                _advance_charge(s, now)
                _sub_merge(oid, {"next_charge": s["next_charge"],
                                 **({"status": "active"} if s.get("status") == "past_due" else {})},
                           remove=("pending_charge_id",))
        else:
            _bump(f"pay_ok_{slug}")
            bg.add_task(_fulfill_paid, email, quiz, oid, base)  # даже при пустом quiz
    return JSONResponse({"ok": True})


# ---------- аккаунт: вход по email (magic-link) + управление подпиской ----------

LOGINS = DATA / "logins.json"


def _find_account(email: str) -> dict:
    """По email найти токен плана и подписку (активная в приоритете)."""
    email = (email or "").strip().lower()
    if not email:
        return {}
    sub = None
    for s in _all_subs():
        if s.get("email", "").strip().lower() == email:
            if s.get("status") == "active":
                sub = s
            elif sub is None:
                sub = s
    if sub and sub.get("plan_token"):
        return {"plan_token": sub["plan_token"], "sub": sub}
    token = ""
    try:
        if ORDERS.exists():
            for line in ORDERS.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                if (r.get("email", "").strip().lower() == email and r.get("order")
                        and (PLANS / f"{r['order']}.json").exists()):
                    token = r["order"]
    except Exception:  # noqa: BLE001
        pass
    return {"plan_token": token, "sub": None} if token else {}


def _logins_load() -> dict:
    try:
        return json.loads(LOGINS.read_text(encoding="utf-8")) if LOGINS.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _logins_save(d: dict) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        LOGINS.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _send_login_email(email: str, link: str) -> None:
    html = (f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;"
            f"padding:32px;color:#20321F'><h2 style='margin:0 0 12px'>Вход в NutriPlan</h2>"
            f"<p style='color:#6B7566'>Нажми кнопку, чтобы открыть свой план и приложение:</p>"
            f"<p style='margin:20px 0'><a href='{link}' style='display:inline-block;background:#16A34A;color:#fff;"
            f"text-decoration:none;font-weight:800;padding:15px 26px;border-radius:14px'>Открыть мой план</a></p>"
            f"<p style='color:#9aa39a;font-size:13px'>Ссылка действует 30 минут. Если вход запрашивал не ты — "
            f"просто проигнорируй это письмо.</p></div>")
    _send_email(email, "Вход в NutriPlan", html, "login")


class LoginReq(BaseModel):
    email: EmailStr
    # Что именно было показано над кнопкой и какой версии — присылает фронт,
    # чтобы в записи лежал реально увиденный текст, а не серверная догадка.
    consent_text: str = ""
    consent_version: str = ""


@app.post("/api/login/request")
def login_request(req: LoginReq, request: Request, bg: BackgroundTasks) -> JSONResponse:
    # Rate-limit: не даём перебирать аккаунты и бомбить почту письмами.
    em = str(req.email).strip().lower()
    if not (_rate_ok("login_email", em, 3, 3600) and _rate_ok("login_ip", _client_ip(request), 15, 3600)):
        return JSONResponse({"ok": True})  # тихо (нейтрально), письмо не шлём
    # Пишем ДО ветвления по наличию аккаунта: согласие человек дал нажатием,
    # независимо от того, нашёлся ли у него план. Ветка «аккаунта нет» тоже
    # обрабатывает его почту — значит и основание на неё нужно.
    _record_consent(request, em, "button_click", req.consent_text, req.consent_version)
    acct = _find_account(str(req.email))
    if acct.get("plan_token"):
        import uuid
        now = datetime.now(timezone.utc).timestamp()
        d = {k: v for k, v in _logins_load().items() if v.get("exp", 0) > now}
        token = uuid.uuid4().hex
        d[token] = {"email": str(req.email), "exp": now + 1800}
        _logins_save(d)
        link = f"{str(request.base_url).rstrip('/')}/login/{token}"
        bg.add_task(_send_login_email, str(req.email), link)
    # Нейтральный ответ ВСЕГДА (есть план или нет) — не раскрываем наличие аккаунта (anti-enum).
    return JSONResponse({"ok": True})


@app.get("/login/{token}", response_class=HTMLResponse)
def login_consume(token: str) -> HTMLResponse:
    token = "".join(c for c in token if c.isalnum())
    now = datetime.now(timezone.utc).timestamp()
    d = _logins_load()
    rec = d.get(token)
    if not rec or rec.get("exp", 0) < now:
        return HTMLResponse("<!doctype html><meta charset='utf-8'>"
            "<div style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h1>Ссылка устарела</h1><p><a href='/login'>Запросить вход заново</a></p></div>", status_code=410)
    d.pop(token, None); _logins_save(d)
    acct = _find_account(rec["email"])
    if acct.get("plan_token"):
        return RedirectResponse(f"/plan/{acct['plan_token']}", status_code=302)
    return HTMLResponse("<!doctype html><meta charset='utf-8'>"
        "<div style='font-family:sans-serif;text-align:center;padding:60px'><h1>План не найден</h1>"
        "<p>По этой почте плана пока нет. <a href='/'>Собрать план</a></p></div>")


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    # через _inject_metrika: страница собирается строкой, а не общим шаблоном,
    # и без этого оставалась единственной без счётчика
    return HTMLResponse(_inject_metrika(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><title>Вход · NutriPlan</title>"
        "<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "background:#FBF8F1;color:#20321F;min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:20px}"
        ".c{max-width:400px;width:100%;text-align:center}.dot{width:11px;height:11px;border-radius:50%;background:#16A34A;"
        "display:inline-block;margin-right:8px}.b{font-weight:800;font-size:20px;margin-bottom:24px}"
        "h1{font-size:24px;letter-spacing:-.02em;margin-bottom:8px}p{color:#6B7566;font-size:15px;margin-bottom:20px}"
        "input{width:100%;border:1.5px solid #EEE7D8;border-radius:14px;padding:15px 17px;font-size:16px;background:#fff}"
        "button{width:100%;margin-top:12px;border:none;border-radius:14px;background:#16A34A;color:#fff;font-weight:800;"
        "font-size:16px;padding:16px;cursor:pointer}.ok{color:#0E7A36;font-weight:700;margin-top:16px;display:none}"
        ".ok.s{display:block}.err{color:#DC2626;font-size:13px;margin-top:8px;display:none}.err.s{display:block}"
        # Подпись над кнопкой — не мелкий шрифт «на отвяжись»: 13px и обычный
        # контраст ссылок, чтобы её нельзя было назвать спрятанной.
        ".cns{font-size:13px;color:#6B7566;line-height:1.45;margin-top:14px;text-align:left}"
        ".cns a{color:#0E7A36}"
        "footer{margin-top:28px;font-size:12.5px;color:#8A9384}footer a{color:#8A9384;margin:0 7px}"
        "</style></head>"
        "<body><div class='c'><div class='b'><span class='dot'></span>NutriPlan</div>"
        "<h1>Вход в приложение</h1><p>Уже есть план? Введи почту — пришлём ссылку для входа.</p>"
        "<input type='email' id='m' placeholder='твой@email.ru' autocomplete='email' inputmode='email'>"
        "<div class='err' id='e'>Проверь адрес почты</div>"
        # Вариант B доктрины: согласие действием. Стоит НЕПОСРЕДСТВЕННО над
        # кнопкой — отдельная галочка, уезжающая из поля зрения, хуже: человек
        # жмёт кнопку, ничего не происходит, и он уходит.
        f"<div class='cns'>{CONSENT_HTML}</div>"
        "<button id='s'>Прислать ссылку для входа</button>"
        "<div class='ok' id='o'>Если на эту почту есть план — письмо со ссылкой для входа уже летит. "
        "Проверь входящие (и «Промоакции»).</div>"
        "<footer><a href='/privacy'>Политика</a><a href='/consent'>Согласие</a><a href='/offer'>Оферта</a></footer>"
        "<script>const $=s=>document.querySelector(s);const ok=v=>/^[^\\s@]+@[^\\s@]+\\.[^\\s@]{2,}$/.test(v);"
        f"const CT={json.dumps(CONSENT_TEXT, ensure_ascii=False)},CV={json.dumps(CONSENT_VERSION)};"
        "$('#s').onclick=async()=>{const v=$('#m').value.trim();if(!ok(v)){$('#e').classList.add('s');return;}"
        "$('#e').classList.remove('s');$('#s').disabled=true;$('#s').textContent='Отправляю…';"
        "try{await fetch('/api/login/request',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({email:v,consent_text:CT,consent_version:CV})});}catch(e){}"
        "$('#o').classList.add('s');$('#s').style.display='none';};"  # нейтрально: не раскрываем наличие аккаунта
        "</script></div></body></html>"))


@app.post("/api/sub/{token}/cancel")
def sub_cancel_api(token: str) -> JSONResponse:
    """Отмена подписки + отвязка карты (требование ЮKassa). Отменяет ВСЕ активные подписки
    этого email — иначе «скрытая» вторая подписка продолжала бы списывать после отмены."""
    sub = _load_sub(token)
    if not sub:
        return JSONResponse({"error": "no sub"}, status_code=404)
    email = (sub.get("email") or "").strip().lower()
    n = 0
    for s in list(_all_subs()):
        same = (s.get("email", "").strip().lower() == email) if email else (s.get("sub_id") == token)
        if same and s.get("status") == "active":
            _sub_merge(s["sub_id"], {"status": "canceled", "payment_method_id": ""})
            n += 1
    if n == 0:  # на всякий — точечно
        _sub_merge(token, {"status": "canceled", "payment_method_id": ""})
    _bump(f"sub_cancel_{sub.get('landing','?')}")
    return JSONResponse({"ok": True, "until": sub.get("next_charge", ""), "canceled": n or 1})


@app.post("/api/sub/{token}/unbind-card")
def sub_unbind_card_api(token: str) -> JSONResponse:
    """Отвязка карты БЕЗ отмены подписки (требование ЮKassa): автосписаний не будет,
    доступ сохраняется до конца оплаченного периода."""
    sub = _load_sub(token)
    if not sub:
        return JSONResponse({"error": "no sub"}, status_code=404)
    sub["payment_method_id"] = ""  # только отвязка способа; status остаётся active до конца периода
    _save_sub(sub)
    _bump(f"card_unbind_{sub.get('landing','?')}")
    return JSONResponse({"ok": True, "until": sub.get("next_charge", "")})


@app.get("/plan/{token}", response_class=HTMLResponse)
def plan_page(token: str) -> HTMLResponse:
    """Веб-страница персонального плана (главный экран PWA) + управление подпиской."""
    safe = "".join(c for c in token if c.isalnum())
    pl = _load_plan(token)
    if not pl and safe in DEMO_TOKENS:
        # Демо-план провизионируется сам. Раньше существовала только демо-ПОДПИСКА,
        # а сам план надо было положить руками — поэтому на любом свежем деплое
        # /plan/sample отдавал 404, и площадка, на которой мы предлагаем
        # безопасно щёлкать интерфейс, просто не открывалась.
        import plan_ai
        pl = plan_ai.generate_plan({})
        pl["quiz"] = {}
        _save_plan(safe, pl)
    if not pl:
        return HTMLResponse("<!doctype html><meta charset='utf-8'>"
            "<div style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h1>План не найден</h1><p>Ссылка устарела или неверна. <a href='/login'>Войти по почте</a></p></div>",
            status_code=404)
    import plan
    if safe in DEMO_TOKENS:
        _save_sub(_demo_sub(safe))  # демо всегда в исходном виде (для проверки ЮKassa)
    sub = _load_sub(safe)
    subinfo = None
    if sub:
        subinfo = {"status": sub.get("status"), "next": sub.get("next_charge", ""),
                   "amount": sub.get("amount", SUB_PRICE_RUB), "has_card": bool(sub.get("payment_method_id"))}
    return HTMLResponse(_inject_metrika(plan.page_html(pl, token=safe, sub=subinfo)))


class SwapReq(BaseModel):
    day: int
    slot: str


@app.post("/api/plan/{token}/swap")
def plan_swap(token: str, req: SwapReq, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Заменить одно блюдо в плане (LLM-регенерация с учётом ограничений)."""
    if not _rate_ok("swap", "".join(c for c in token if c.isalnum())[:40], 20, 3600):
        return JSONResponse({"error": "too many"}, status_code=429)  # anti LLM-cost-abuse
    pl = _load_plan(token)
    days = pl.get("days") or []
    if not (0 <= req.day < len(days)):
        return JSONResponse({"error": "bad day"}, status_code=400)
    meals = days[req.day].get("meals") or []
    idx = next((i for i, m in enumerate(meals) if m.get("slot") == req.slot), -1)
    if idx < 0:
        return JSONResponse({"error": "no meal"}, status_code=404)
    old = meals[idx]
    import plan_ai
    new = plan_ai.swap_meal(pl.get("quiz") or {}, req.slot, old.get("kcal") or 400, old.get("name", ""))
    if not new:
        return JSONResponse({"error": "gen failed"}, status_code=502)
    meals[idx] = new
    _save_plan("".join(c for c in token if c.isalnum()), pl)
    bg.add_task(dish_photos.generate, dish_photos.slugify(new.get("name", "")), new.get("name", ""))
    return JSONResponse({"meal": new})


class SettingsReq(BaseModel):
    exclude: str | None = None      # что не ем (продукты, через запятую)
    weight: float | None = None     # текущий вес → пересчёт нормы КБЖУ
    goal: str | None = None         # lose|keep|gain|health
    meals: str | None = None        # 2|3|3s|if
    cook: str | None = None         # q15|q30|any|none
    favorites: list[str] | None = None
    dislike: str | None = None      # добавить ОДНО блюдо в стоп-лист


@app.post("/api/plan/{token}/settings")
def plan_settings(token: str, req: SettingsReq, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Изменить настройки плана (вес/цель/приёмы/готовка/любимое/исключения/дизлайк) и пересобрать.
    Норма КБЖУ пересчитывается автоматически (generate_plan → compute по новому quiz)."""
    if not _rate_ok("settings", "".join(c for c in token if c.isalnum())[:40], 15, 3600):
        return JSONResponse({"error": "too many"}, status_code=429)  # anti LLM-cost-abuse
    pl = _load_plan(token)
    if not pl:
        return JSONResponse({"error": "no plan"}, status_code=404)
    quiz = dict(pl.get("quiz") or {})
    if req.exclude is not None:
        quiz["exclude"] = (req.exclude or "").strip()[:300]
    if req.goal in ("lose", "keep", "gain", "health"):
        quiz["goal"] = req.goal
    if req.meals in ("2", "3", "3s", "if"):
        quiz["meals"] = req.meals
    if req.cook in ("q15", "q30", "any", "none"):
        quiz["cook"] = req.cook
    if req.favorites is not None:
        quiz["favorites"] = [f for f in req.favorites if f in ("meat", "fish", "veg", "grain", "dairy", "sweet")]
    if req.weight is not None and 30 <= req.weight <= 350:
        body = dict(quiz.get("body") or {})
        body["weight"] = round(float(req.weight), 1)
        quiz["body"] = body
    if req.dislike:
        dis = list(quiz.get("disliked") or [])
        d = req.dislike.strip()[:80]
        if d and d.lower() not in [x.lower() for x in dis]:
            dis.append(d)
        quiz["disliked"] = dis[-40:]  # ограничим накопление
    import plan_ai
    new = plan_ai.generate_plan(quiz)
    new["quiz"] = quiz
    safe = "".join(c for c in token if c.isalnum())
    _save_plan(safe, new, reset_progress=True)  # план пересобран → отметки «приготовил» неактуальны
    bg.add_task(_pregen_dish_photos, new)
    return JSONResponse({"ok": True, "cal": new.get("cal")})


@app.get("/sw.js")
def service_worker() -> Response:
    js = (STATIC / "sw.js").read_text(encoding="utf-8")
    return Response(js, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/app.webmanifest")
def webmanifest(t: str = "") -> Response:
    t = "".join(c for c in t if c.isalnum())
    m = {
        "name": "NutriPlan — план питания", "short_name": "NutriPlan",
        "start_url": f"/plan/{t}" if t else "/", "scope": "/", "display": "standalone",
        "background_color": "#FBF8F1", "theme_color": "#16A34A", "orientation": "portrait",
        "icons": [
            {"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return Response(json.dumps(m, ensure_ascii=False), media_type="application/manifest+json")


@app.get("/api/plan/{token}/ready")
def plan_ready(token: str) -> JSONResponse:
    """Готов ли план (для поллинга success-страницы после оплаты)."""
    tok = "".join(c for c in token if c.isalnum())
    return JSONResponse({"ready": bool(tok and (PLANS / f"{tok}.json").exists())})


def _pay_unknown_page() -> HTMLResponse:
    """Заказ или платёж не нашлись — API молчит, ссылка старая, заказа нет.
    Раньше в этом случае показывалось «Оплата получена!»: мы утверждали факт
    списания, ничего о нём не зная. Лучше честно признать неопределённость и
    дать канал связи, чем угадать в пользу приятного варианта."""
    return HTMLResponse(_inject_metrika(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Не нашли платёж · NutriPlan</title>"
        "<style>html,body{margin:0;background:#FBF8F1}</style>"
        "</head><body>"
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;"
        "min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;"
        "text-align:center;padding:30px;background:#FBF8F1;color:#20321F\">"
        "<div style='width:88px;height:88px;border-radius:50%;background:#FEF3C7;display:flex;"
        "align-items:center;justify-content:center'><svg width='40' height='40' viewBox='0 0 24 24' "
        "fill='none' stroke='#B98900' stroke-width='3' stroke-linecap='round'><path d='M12 8v5'/>"
        "<path d='M12 17h.01'/><circle cx='12' cy='12' r='9'/></svg></div>"
        "<h1 style='margin:22px 0 8px;font-size:26px'>Не нашли этот платёж</h1>"
        "<p style='color:#6B7566;font-size:16px;max-width:36ch'>Ссылка могла устареть. Если деньги "
        "списались — план придёт на почту, ничего делать не нужно. Если списания не было, попробуй "
        "оформить заново.</p>"
        "<a href='/quiz' style='margin-top:22px;display:inline-block;background:#16A34A;color:#fff;"
        "text-decoration:none;font-weight:800;padding:15px 28px;border-radius:14px'>К плану</a>"
        "<p style='color:#8A9384;font-size:13px;margin-top:16px'>Вопросы — "
        "<a href='mailto:support@mynutriplan.ru' style='color:#0E7A36'>support@mynutriplan.ru</a></p>"
        "</div></body></html>"), status_code=200)


def _pay_failed_page(base_url_hint: str = "") -> HTMLResponse:
    return HTMLResponse(_inject_metrika(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Оплата не прошла · NutriPlan</title>"
        "<style>html,body{margin:0;background:#FBF8F1}</style>"
        "</head><body>"
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;"
        "min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;"
        "text-align:center;padding:30px;background:#FBF8F1;color:#20321F\">"
        "<div style='width:88px;height:88px;border-radius:50%;background:#FEE2E2;display:flex;align-items:center;"
        "justify-content:center'><svg width='42' height='42' viewBox='0 0 24 24' fill='none' stroke='#DC2626' "
        "stroke-width='3' stroke-linecap='round'><path d='M18 6L6 18M6 6l12 12'/></svg></div>"
        "<h1 style='margin:22px 0 8px;font-size:26px'>Оплата не прошла</h1>"
        "<p style='color:#6B7566;font-size:16px;max-width:34ch'>Деньги не списаны. Возможно, банк отклонил "
        "платёж или ты закрыл окно оплаты. Попробуй ещё раз — это займёт минуту.</p>"
        "<a href='/quiz' style='margin-top:22px;display:inline-block;background:#16A34A;color:#fff;"
        "text-decoration:none;font-weight:800;padding:15px 28px;border-radius:14px'>Попробовать снова</a></div>"
        "</body></html>"),
        status_code=200)


@app.get("/pay/success", response_class=HTMLResponse)
def pay_success(o: str = "") -> HTMLResponse:
    """Возврат с ЮKassa. Экран определяется ПЕРЕПРОВЕРЕННЫМ статусом платежа,
    а не фактом редиректа: ЮKassa возвращает сюда и когда человек просто закрыл
    окно оплаты.

    Раньше «Оплата получена!» показывалась во всех случаях, кроме явного
    `canceled` — то есть при `pending`, при недоступном API и при ненайденном
    заказе человеку сообщали, что деньги получены, когда их не было. Это ложь
    о деньгах, худший вид ошибки в оплате.

    Состояния:
      succeeded / план уже есть → «Оплата получена», поллим и открываем план
      pending, waiting_for_capture → «Платёж обрабатывается», поллим (может
                                     дозавершиться), но НЕ утверждаем оплату
      canceled                    → «Оплата не прошла»
      всё остальное               → «Не нашли платёж» — честнее, чем гадать
    """
    oid = "".join(c for c in (o or "") if c.isalnum())[:40]
    order = _find_order(oid) if oid else {}
    pid = order.get("payment_id", "")
    st = _yk_get_payment(pid).get("status", "") if pid else ""
    plan_ready = bool(oid) and (PLANS / f"{oid}.json").exists()
    pay_url = order.get("pay_url", "")

    if st == "canceled" and not plan_ready:
        return _pay_failed_page()
    if not plan_ready and st not in ("succeeded", "pending", "waiting_for_capture"):
        # Нет заказа, нет платежа или API молчит. Не выдумываем статус.
        return _pay_unknown_page()

    paid = plan_ready or st == "succeeded"

    poll = ""
    if oid:
        poll = (
            "<script>(function(){var t=0;"
            f"var u='/api/plan/{oid}/ready',p='/plan/{oid}';"
            "function tick(){t+=3;fetch(u).then(function(r){return r.json()}).then(function(j){"
            "if(j.ready){location.href=p;return;}"
            "if(t<240){setTimeout(tick,3000);}else{"
            "document.getElementById('wait').innerHTML='План почти готов — отправили ссылку на почту. "
            "Проверь входящие (и \\u00abПромоакции\\u00bb).';}"
            "}).catch(function(){setTimeout(tick,5000);});}"
            "setTimeout(tick,3000);})();</script>")

    # Цель оплаты — только при ПЕРЕПРОВЕРЕННОМ succeeded. Сам факт редиректа
    # с ЮKassa ничего не значит: она редиректит и при отмене. Защита от
    # повторов — sessionStorage по заказу, иначе обновление страницы (а тут
    # страница живёт минуту и поллит) накрутило бы конверсию.
    paid_goal = ""
    if oid and st == "succeeded":
        paid_goal = (
            "<script>(function(){try{var k='np_paid_" + oid + "';"
            "if(!sessionStorage.getItem(k)){sessionStorage.setItem(k,'1');"
            "if(window.npGoal)window.npGoal('pay_success');}}catch(e){}})();</script>")

    # Пока платёж висит в pending, ссылка ЮKassa ещё жива — человеку, который
    # закрыл окно и передумал, надо дать вернуться туда же, а не проходить всё
    # заново. Ссылка живёт не вечно, поэтому рядом всегда есть запасной путь.
    actions = ""
    if not paid:
        back = (f"<a href=\"{pay_url}\" style=\"display:inline-block;background:#16A34A;color:#fff;"
                "text-decoration:none;font-weight:800;padding:15px 28px;border-radius:14px\">"
                "Вернуться к оплате</a>") if pay_url else ""
        again = ("<a href=\"/quiz\" style=\"display:block;margin-top:14px;color:#6B7566;font-size:14px;"
                 "text-decoration:underline;text-underline-offset:3px\">Оформить заново</a>")
        actions = f"<div style=\"margin-top:22px\">{back}{again}</div>"

    title = "Оплата получена!" if paid else "Платёж обрабатывается"
    body = ("Авокадо собирает твой план (≈1 минута) — страница откроет его сама. "
            "Ссылка придёт и на почту.") if paid else (
            "Банк ещё не подтвердил платёж. Если деньги спишутся, план соберётся "
            "автоматически и ссылка придёт на почту — эту страницу можно закрыть.")
    mark = ("<svg width='44' height='44' viewBox='0 0 24 24' fill='none' stroke='#fff' stroke-width='3' "
            "stroke-linecap='round' stroke-linejoin='round'><path d='M5 13l4 4L19 7'/></svg>") if paid else (
            "<svg width='40' height='40' viewBox='0 0 24 24' fill='none' stroke='#fff' stroke-width='3' "
            "stroke-linecap='round'><path d='M12 7v5l3 2'/><circle cx='12' cy='12' r='9'/></svg>")
    ring_bg = "#16A34A" if paid else "#B98900"

    return HTMLResponse(_inject_metrika(
        # <head> здесь настоящий, а не для красоты: _inject_metrika вставляет
        # счётчик ПЕРЕД </head>, и без него страница возврата оставалась без
        # аналитики — слепое пятно №1 из доктрины add-payments: деньги дошли,
        # а Метрика и Директ об этом не узнали.
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{title} · NutriPlan</title>"
        "<style>html,body{margin:0;background:#FBF8F1}</style>"
        "</head><body>"
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;"
        "min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;"
        "text-align:center;padding:30px;background:#FBF8F1;color:#20321F\">"
        f"<div style='width:88px;height:88px;border-radius:50%;background:{ring_bg};display:flex;"
        f"align-items:center;justify-content:center;box-shadow:0 24px 50px -20px {ring_bg}'>{mark}</div>"
        f"<h1 style='margin:22px 0 8px;font-size:27px'>{title}</h1>"
        f"<p id='wait' style='color:#6B7566;font-size:16px;max-width:36ch'>{body}</p>"
        + actions +
        "<div style='margin-top:18px;width:34px;height:34px;border:3px solid #DCFCE7;border-top-color:#16A34A;"
        "border-radius:50%;animation:sp 1s linear infinite'></div>"
        "<style>@keyframes sp{to{transform:rotate(360deg)}}</style>"
        + poll + paid_goal + "</div></body></html>"))


@app.get("/api/cron/run")
def cron_run(request: Request, secret: str = "") -> JSONResponse:
    """Тик планировщика: недельная регенерация плана + месячное списание. Дёргать системным cron.
    Секрет — через заголовок X-Cron-Secret (предпочтительно) или ?secret= (совместимость)."""
    if not _secret_ok(secret, request, "NUTRI_CRON_SECRET", "X-Cron-Secret"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    import plan
    import plan_ai
    now = datetime.now(timezone.utc)
    base = str(request.base_url).rstrip("/")
    out = {"checked": 0, "replanned": 0, "billed": 0, "pending": 0, "retrying": 0, "past_due": 0, "demo_skipped": 0}
    grace = timedelta(days=3)  # окно ретраев после неудачного списания, потом past_due
    for sub in _all_subs():
        # демо-подписки (для проверяющих ЮKassa) не биллим и не регенерим — иначе
        # cron спишет с фейковой карты, провалится и покажет демо как past_due
        if (sub.get("sub_id") in DEMO_TOKENS) or (sub.get("plan_token") in DEMO_TOKENS):
            out["demo_skipped"] += 1
            continue
        if sub.get("status") != "active":
            continue
        out["checked"] += 1
        sid = sub["sub_id"]
        upd: dict = {}      # только cron-поля, пишем через merge (не затираем cancel/unbind)
        rem: list = []
        try:  # недельная регенерация плана (тот же токен → PWA показывает свежую неделю)
            if now >= datetime.fromisoformat(sub["next_plan"]):
                pl = plan_ai.generate_plan(sub.get("quiz") or {})
                pl["quiz"] = sub.get("quiz") or {}
                _save_plan(sub["plan_token"], pl, reset_progress=True)  # новый план → сброс «приготовил»
                link = f"{base}/plan/{sub['plan_token']}"
                _send_email(sub["email"], "Новый план на неделю · NutriPlan",
                            plan.menu_email_html(pl, link), "plan_weekly")
                upd["next_plan"] = (datetime.fromisoformat(sub["next_plan"]) + timedelta(days=7)).isoformat()
                out["replanned"] += 1
        except Exception:  # noqa: BLE001
            pass
        try:  # месячное списание (pending-aware + grace-ретраи + честная обработка отвязки)
            pend = sub.get("pending_charge_id")
            due = now >= datetime.fromisoformat(sub["next_charge"])
            if pend:
                st = _yk_payment_status(pend)  # досматриваем незакрытый платёж, новый НЕ создаём
                if st == "succeeded":
                    if _advance_charge(sub, now):
                        upd["next_charge"] = sub["next_charge"]; out["billed"] += 1
                    rem.append("pending_charge_id")
                elif st == "canceled":
                    rem.append("pending_charge_id")  # провал → grace-логика на след. тике
                # иначе pending — ждём
            elif due and not sub.get("payment_method_id"):
                # Карта отвязана юзером (unbind) — автопродление невозможно. По истечении
                # периода честно ЗАВЕРШАЕМ подписку. НЕ «банк отклонил», НЕ grace-ретраи.
                if _guarded_status(sid, "ended"):
                    out["past_due"] += 1  # (в счётчике «завершённые по отвязке»)
                    _send_email(sub["email"], "Подписка завершена · NutriPlan",
                                _sub_ended_email(base, unbound=True), "sub_ended")
            elif due:
                status, pid = _charge_subscription(sub)
                if status == "succeeded":
                    if _advance_charge(sub, now):
                        upd["next_charge"] = sub["next_charge"]
                    out["billed"] += 1
                elif status == "pending":
                    upd["pending_charge_id"] = pid; out["pending"] += 1  # НЕ провал
                else:  # failed — ретраим до grace, потом past_due + письмо
                    if now > datetime.fromisoformat(sub["next_charge"]) + grace:
                        if _guarded_status(sid, "past_due"):
                            out["past_due"] += 1
                            _send_email(sub["email"], "Не удалось продлить подписку · NutriPlan",
                                        _sub_ended_email(base, unbound=False), "sub_past_due")
                    else:
                        out["retrying"] += 1
        except Exception:  # noqa: BLE001
            pass
        if upd or rem:
            _sub_merge(sid, upd, remove=tuple(rem))
    # Самолечение планов: упавшие в bank-fallback (LLM сбоил — без рецептов/покупок)
    # дорегенерируем. Кап на тик — ограничить LLM-затраты; демо не трогаем.
    out["repaired"] = 0
    for f in sorted(PLANS.glob("*.json")):
        if out["repaired"] >= 3:
            break
        try:
            if f.stem in DEMO_TOKENS:
                continue
            pl = json.loads(f.read_text(encoding="utf-8"))
            if pl.get("source") != "bank" or not pl.get("quiz"):
                continue
            fresh = plan_ai.generate_plan(pl["quiz"])
            if fresh.get("source") == "ai":  # апгрейд только на полноценный план
                fresh["quiz"] = pl["quiz"]
                _save_plan(f.stem, fresh)
                out["repaired"] += 1
        except Exception:  # noqa: BLE001
            pass
    return JSONResponse(out)


@app.get("/sub/cancel", response_class=HTMLResponse)
def sub_cancel(s: str = "") -> HTMLResponse:
    """GET безопасен (не мутирует!) — отмена только через POST /api/sub/{token}/cancel с
    двойным подтверждением в ЛК. Раньше GET отменял подписку побочным эффектом (любой
    префетч/сканер по публичной ссылке мог отменить)."""
    tok = "".join(c for c in (s or "") if c.isalnum())
    if tok and _load_sub(tok):
        return RedirectResponse(f"/plan/{tok}#sub", status_code=302)
    return HTMLResponse("<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<div style='font-family:sans-serif;text-align:center;padding:60px;color:#20321F'>"
        "<h1>Управление подпиской — в приложении</h1>"
        "<p>Открой свой план и найди раздел «Подписка». <a href='/login'>Войти по почте</a></p></div>")


@app.get("/admin/stats")
def admin_stats(request: Request, token: str = "") -> JSONResponse:
    if not _secret_ok(token, request, "ADMIN_TOKEN", "X-Admin-Token"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    c = _counters()
    rows = []
    for slug, name in LANDINGS.items():
        v = c.get(f"visit_{slug}", 0)
        leads = c.get(f"lead_{slug}", 0)
        rows.append({
            "landing": slug, "title": name, "visits": v, "leads": leads,
            "lead_rate": round(leads / v * 100, 2) if v else 0.0,
            "visits_ad": c.get(f"visit_{slug}_ad", 0),
            "visits_organic": c.get(f"visit_{slug}_organic", 0),
        })
    return JSONResponse({"landings": rows})
