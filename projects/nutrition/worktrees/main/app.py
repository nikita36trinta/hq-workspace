"""
Nutrition — скелет бэкенда под тест 4 лендингов (см. STRUCTURE.md).

Один продукт (персональный план питания), 8 лендингов под разные углы. На каждый
льём отдельный трафик, сравниваем конверсию. Движок квиза/лида/статистики
переиспользуется из astro + sdelka. Оплата БОЕВАЯ (ЮKassa, shop 1411445): разовый
299₽ + подписка 499₽/мес с автосписанием (рекурренты одобрены 2026-07-22).
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from starlette.concurrency import run_in_threadpool

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
    # Схема из X-Forwarded-Proto. Мы отключили ProxyHeaders-мидлварь uvicorn
    # (она доверяла подделываемому левому элементу X-Forwarded-For), но вместе с
    # ней потерялся и учёт протокола: request.base_url стал всегда http://.
    # А на нём собираются return_url для ЮKassa и ВСЕ ссылки в письмах — то есть
    # человек получал http-ссылки, лишний редирект в момент возврата после
    # оплаты и спам-сигнал в почте. Заголовку верим по тому же правилу, что и
    # адресу: только от пира из доверенного списка.
    try:
        peer = request.client.host if request.client else ""
        proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if proto in ("http", "https") and _peer_trusted(peer):
            request.scope["scheme"] = proto
    except Exception:  # noqa: BLE001
        pass
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
    if not on:
        # Метки рекламного клика (см. _remember_marks) — тем же проходом, чтобы
        # ловились на ЛЮБОМ входе: /l/*, /quiz, голый домен с ?utm_source=…
        _remember_marks(request, response)
    return response


def notrack() -> bool:
    return _notrack.get()


# ── Потолок тела запроса ─────────────────────────────────────────────────────
# POST /api/lead был открыт настежь: одно тело на 500 КБ ложилось в leads.jsonl
# целиком (проверено — файл вырос на 500 148 байт за один запрос), и упереться
# было не во что. Режем на входе, ДО эндпоинта: pydantic успевает распарсить
# мегабайты раньше, чем мы что-то решим.
#
# Настоящий квиз укладывается в ~1 КБ, вебхук ЮKassa — в единицы килобайт,
# поэтому 64 КБ живой человек не увидит никогда.
MAX_BODY_BYTES = 64 * 1024


class _BodyLimit:
    """ASGI-прослойка: тело больше лимита → 413 без разбора."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        for k, v in scope.get("headers") or ():
            if k == b"content-length":
                try:
                    if int(v) > self.max:
                        return await self._too_big(send)
                except ValueError:
                    pass
        seen = 0

        async def _receive():
            nonlocal seen
            msg = await receive()
            if msg.get("type") == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > self.max:
                    # Chunked без Content-Length: обрываем поток. Тело не соберётся
                    # в валидный JSON → 422, на диск не попадёт ничего.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return msg

        await self.app(scope, _receive, send)

    async def _too_big(self, send):
        body = b'{"error":"too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(_BodyLimit)


STATIC = Path(__file__).parent / "static"
# картинки контента (Nano Banana) + прочие статик-ассеты лендингов
(STATIC / "assets").mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(STATIC / "assets")), name="assets")
DATA = Path(os.getenv("DATA_DIR", "/app/data"))
LEADS = DATA / "leads.jsonl"
COUNTERS = DATA / "counters.json"
ORDERS = DATA / "orders.jsonl"  # PII — только в DATA (gitignore), не коммитить
PLANS = DATA / "plans"          # сгенерированные планы: {token}.json

# Аккаунты, пароли и сессии — в SQLite, а не в json рядом с остальным.
# Причина в auth.py: на json-хранилищах уже сгорели счётчики и идемпотентность
# платежей, а сессии пишутся чаще всего остального вместе взятого.
import auth as _auth                                                  # noqa: E402
AUTH = _auth.Auth(os.getenv("NUTRI_DB_PATH", str(DATA / "nutriplan.sqlite3")))
SESSION_COOKIE = _auth.COOKIE_NAME
COOKIE_SECURE = os.getenv("NUTRI_COOKIE_SECURE", "1") != "0"   # 0 для локального http
PAY_ORDER_COOKIE = "np_o"  # заказ, по которому открыта страница возврата (см. pay_success)

# ЮKassa (ключи только из .env боевого сервера; без них — оплата отдаёт 503)
YOOKASSA_SHOP = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
PRICE_RUB = os.getenv("NUTRI_PRICE_RUB", "299")          # разовый план
SUB_PRICE_RUB = os.getenv("NUTRI_SUB_PRICE_RUB", "499")  # подписка / мес
CRON_SECRET = os.getenv("NUTRI_CRON_SECRET", "").strip() # защита cron-эндпоинта
# Базовый адрес для ссылок в письмах, когда под рукой нет request (фоновое дозаказывание
# плана): ссылка на localhost в письме клиенту хуже, чем захардкоженный прод-домен.
PUBLIC_BASE = os.getenv("NUTRI_BASE_URL", "https://mynutriplan.ru").rstrip("/")
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


# Поля, привязанные к ТОКЕНУ, а не к конкретной версии меню: при регенерации плана
# они переезжают в новую версию. Иначе started уехал бы на дату регенерации, а
# письма-возвраты ушли бы по второму разу.
# mail_owed — письмо, которое мы по этому плану ЗАДОЛЖАЛИ (см. _OWED_MAIL). Оно тоже
# сквозное: долг ставится на деградированном плане и гасится дорегенерацией, то есть
# ОБЯЗАН пережить перезапись плана, ради которой всё и затевалось.
_PLAN_STICKY = ("started", "mail_d2", "mail_d6", "mail_owed")

# Письмо, отложенное до успешной дорегенерации: ключ → (тема, тег мейлера).
# Банк-заготовка — это меню без рецептов и списка покупок, то есть ровно без того,
# что продавал пейволл. Слать по ней «Твой план готов» значит соврать о товаре,
# поэтому письмо ждёт, пока cron соберёт полноценный план (см. cron_run).
_OWED_MAIL = {
    "paid":   ("Твой план на 7 дней · NutriPlan", "plan_paid"),
    "weekly": ("Новый план на неделю · NutriPlan", "plan_weekly"),
    "requiz": ("Новый план по твоим ответам · NutriPlan", "plan_requiz"),
}


def _menu_names(pl: dict) -> list[str]:
    """Названия блюд плана по порядку. Ими отвечаем на два вопроса: «меню вообще
    изменилось?» (иначе письмо «новый план» рекламирует ту же неделю) и «что не
    повторять» — этот же список уходит в generate_plan(avoid=…)."""
    return [(m.get("name") or "") for d in (pl.get("days") or []) for m in (d.get("meals") or [])]


def _append_jsonl(path: Path, rec: dict, what: str) -> bool:
    """Дописать строку в jsonl. Сбой НЕ глотаем: молча потерянный лид или заказ —
    это потерянные деньги, о которых никто никогда не узнает (раньше здесь стоял
    except: pass, и диск, кончившийся на проде, выглядел бы как «лидов нет»)."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        _bump(f"write_fail_{what}")
        print(f"[ALERT] не записан {what} в {path}: {e}", flush=True)
        return False


def _write_plan(token: str, pl: dict) -> None:
    try:
        PLANS.mkdir(parents=True, exist_ok=True)
        (PLANS / f"{token}.json").write_text(json.dumps(pl, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        # План — это то, за что заплатили. Тихо не сохранить его нельзя.
        _bump("write_fail_plan")
        print(f"[ALERT] план {token} НЕ сохранён: {e}", flush=True)
        return
    # Блюда нового плана сразу становятся известными (см. _index_dish_slugs) —
    # иначе первое фото свежей замены ждало бы протухания кэша промахов.
    _index_dish_slugs(pl)


def _plan_mark(token: str, fields: dict) -> None:
    """Служебная пометка в файле плана (письма, деградация) БЕЗ роста ver: ver считает
    версии меню, к которым клиент привязывает отметки «приготовил»."""
    safe = "".join(c for c in token if c.isalnum())
    pl = _load_plan(safe)
    if not pl:
        return
    pl.update(fields)
    _write_plan(safe, pl)


def _save_plan(token: str, pl: dict, reset_progress: bool = False) -> None:
    safe = "".join(c for c in token if c.isalnum())  # как в _load_plan, иначе сохраним не туда
    prev = _load_plan(safe)
    now = datetime.now(timezone.utc).isoformat()
    # ver+started: клиенту нужно знать, к какой версии меню относятся его отметки
    # «приготовил» — иначе серверный сброс прогресса он тут же перезатирает локальным
    # состоянием. started — дата первого сохранения токена, при регенерации не меняется.
    try:
        old_ver = int(prev.get("ver") or 0)
    except Exception:  # noqa: BLE001
        old_ver = 0
    # ver — номер ПОКОЛЕНИЯ МЕНЮ, а не номер записи на диск: к нему клиент привязывает
    # localStorage-ключ отметок «приготовил». Замена одного блюда (swap) сохраняет тот же
    # план — подними мы там ver, у человека на ровном месте обнулился бы ключ со всеми
    # отметками недели. Новое меню = reset_progress.
    pl["ver"] = old_ver + 1 if (reset_progress or not old_ver) else old_ver
    for k in _PLAN_STICKY:
        if prev.get(k) and not pl.get(k):
            pl[k] = prev[k]
    pl.setdefault("started", now)
    # Банк-заготовка — план БЕЗ рецептов и списка покупок, то есть без того, что
    # продаёт пейволл. Молча отдавать её нельзя: помечаем и считаем, cron дорегенерирует
    # (пометка снимется сама — успешная перегенерация приходит новым словарём).
    if pl.get("source") != "ai" and safe not in DEMO_TOKENS:
        pl["degraded"] = True
        pl.setdefault("degraded_at", now)
        if not prev.get("degraded"):
            _bump("plan_degraded")
            print(f"[ALERT] план {safe} сохранён деградированным (source={pl.get('source')!r}): "
                  f"без рецептов и списка покупок", flush=True)
    _write_plan(safe, pl)
    if reset_progress:  # новая версия плана → блюда не должны быть уже «приготовлены»
        try:
            pr = _load_progress(token)
            pr["done"] = {}  # воду/вес сохраняем — это трекеры, а не отметки блюд
            pr.setdefault("water", {})
            pr.setdefault("weight", [])  # форма ответа для клиента не должна меняться
            # Штамп версии, к которой относится сброс: без него клиент видит пустой
            # «приготовил» с сервера, считает его отставшим и возвращает свои старые
            # отметки — сброс отменяется сам собой.
            pr["ver"] = pl["ver"]
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
    """ver — версия плана, к которой относятся отметки «приготовил» (см. _save_plan):
    клиенту она нужна, чтобы отличить серверный сброс от отставшего состояния."""
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
    if prev.get("ver") is not None:
        data["ver"] = prev["ver"]  # версию штампует сервер при сбросе, клиент её не переписывает
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
        degraded = pl.get("source") != "ai"
        if degraded:
            # Оплачено, а отдаём банк-заготовку: ни рецептов, ни списка покупок —
            # ровно то, что продавал пейволл. Метку в плане ставит _save_plan,
            # здесь важно, КОМУ именно это ушло.
            _bump("fulfill_degraded")
            print(f"[ALERT] оплаченный план деградирован: order={oid} email={email} "
                  f"source={pl.get('source')!r}", flush=True)
        if oid:
            _save_plan(oid, pl)
            if degraded:
                # Долг по письму: настоящее «Твой план на 7 дней» уйдёт из cron сразу
                # после успешной дорегенерации. Метка — в файле плана, а не в памяти:
                # рестарт контейнера не должен превращать долг в «никто ничего не должен».
                _plan_mark(oid, {"mail_owed": "paid"})
        # Аккаунт заводится молча, в момент доставки оплаченного. Человек ничего
        # для этого не делает и никакого экрана не видит: почта уже известна из
        # квиза, а новое обязательное поле на пути к оплаченному товару — прямая
        # потеря тех, кто уже заплатил.
        try:
            if _auth.valid_email(email):
                AUTH.ensure_account(email)
        except Exception as e:  # noqa: BLE001
            # Не даём этому уронить доставку: план и письмо важнее аккаунта,
            # а завести его можно и позже, при первом входе.
            print(f"[ALERT] не удалось завести аккаунт для {email}: {e}", flush=True)
        link = f"{base}/plan/{oid}" if oid else ""
        if degraded:
            # «План готов» по заготовке без рецептов — обещание того, чего в плане нет.
            # Честный статус + второе письмо после дорегенерации (см. cron_run).
            _send_email(email, "Собираем твой план · NutriPlan", _plan_wait_email(base, link),
                        "plan_paid_wait")
        else:
            _send_email(email, "Твой план на 7 дней · NutriPlan", plan.menu_email_html(pl, link), "plan_paid")
        _pregen_dish_photos(pl)  # фото блюд заранее — к открытию плана уже готовы (общий кэш)
    except Exception as e:  # noqa: BLE001
        _bump("fulfill_fail")
        print(f"[ALERT] fulfill failed: order={oid} err={e}", flush=True)


def _plan_missing(oid: str) -> bool:
    """Файла плана по заказу нет. Единственный честный признак «оплачено, но не отдано»:
    отметка об обработке платежа этого не показывает — её ставили ДО генерации."""
    safe = "".join(c for c in (oid or "") if c.isalnum())
    return not (safe and (PLANS / f"{safe}.json").exists())


def _fulfill_once(email: str, quiz: dict, oid: str, base: str) -> bool:
    """Идемпотентная доставка оплаченного плана: один план и одно письмо на заказ,
    но при СБОЕ попытка повторяется.

    Раньше единственной защитой была отметка payment_id, которую ставили ДО генерации:
    исключение внутри _fulfill_paid (оно глушится) или перезапуск контейнера — и человек
    оставался без плана навсегда, потому что повторную доставку вебхука отбрасывали как
    дубль. Теперь «уже сделано» = файл плана существует, а не «мы начинали»; на время
    самой генерации держим короткую аренду (_claim), чтобы параллельные заходы
    (вебхук + /pay/success + cron) не собрали два плана и не отправили два письма.
    Возвращает True, если план в итоге есть."""
    if not _plan_missing(oid):
        return True
    key = f"fulfill:{''.join(c for c in (oid or '') if c.isalnum()) or email}"
    if not _claim(key, 300):
        return False  # генерацию уже ведёт другой заход — второй план и второе письмо не нужны
    _fulfill_paid(email, quiz, oid, base)
    if _plan_missing(oid):
        # Аренда нужна только на ВРЕМЯ генерации. Если она закончилась без плана, держать
        # ключ ещё 5 минут — значит запретить немедленный ретрай (повтор вебхука, заход на
        # /pay/success) ровно тогда, когда он и нужен.
        _unclaim(key)
        return False
    return True


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


def _guarded_status(sid: str, new_status: str, only: tuple = ("active",)) -> bool:
    """Сменить статус подписки ТОЛЬКО если текущий входит в `only` (по умолчанию — active):
    не затираем отмену/завершение, случившиеся параллельно, пока cron думал.
    Возвращает True, если применено."""
    cur = _load_sub(sid)
    if not cur or cur.get("status") not in only:
        return False
    _sub_merge(sid, {"status": new_status})
    return True


_RU_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")


def _ru_date(iso: str) -> str:
    """Дата для письма — в московском времени: храним всё в UTC, а человек сверяет
    дату списания с выпиской банка, и разница в 3 часа даёт разные сутки."""
    try:
        d = datetime.fromisoformat(iso)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        d = d.astimezone(timezone(timedelta(hours=3)))
        return f"{d.day} {_RU_MONTHS[d.month - 1]}"
    except Exception:  # noqa: BLE001
        return ""


def _mail_shell(head: str, body: str, cta_href: str = "", cta_label: str = "", foot: str = "") -> str:
    cta = (f"<p style='margin:20px 0'><a href='{cta_href}' style='display:inline-block;background:#16A34A;"
           f"color:#fff;text-decoration:none;font-weight:800;padding:15px 26px;border-radius:14px'>"
           f"{cta_label}</a></p>") if (cta_href and cta_label) else ""
    return (f"<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;margin:0 auto;"
            f"padding:32px;color:#20321F'><h2 style='margin:0 0 12px'>{head}</h2>"
            f"<div style='color:#6B7566;font-size:15px;line-height:1.55'>{body}</div>{cta}{foot}</div>")


def _cancel_note(base: str, sid: str) -> str:
    return (f"<p style='color:#8A9384;font-size:13px;margin-top:18px'>Отменить подписку можно в любой момент — "
            f"<a href='{base}/sub/cancel?s={sid}' style='color:#8A9384'>управление подпиской</a>.</p>")


def _sub_ended_email(base: str, reason: str) -> str:
    """Письмо в день, когда подписка реально закончилась.

    Три причины, и путать их нельзя: «ты отвязал карту», «банк не подтвердил
    списание» и «ты сам отменил» — это три разных разговора, и человек, который
    ничего не отменял, не должен читать про свою отмену.
    """
    heads = {"unbound": "Подписка завершена",
             "failed": "Подписка приостановлена",
             "canceled": "Подписка завершена"}
    bodies = {
        "unbound": "Автопродление отключено (ты отвязал карту) — доступ к плану сохранён. "
                   "Хочешь снова получать свежие планы каждую неделю? Оформи подписку заново:",
        "failed": "Не получилось продлить подписку — банк не подтвердил списание. Твой план сохранён. "
                  "Оформи подписку заново, чтобы продолжить получать свежие планы:",
        "canceled": "Оплаченный период закончился — новые недели больше не приходят. Последний "
                    "план остаётся по прежней ссылке, по нему можно готовить дальше. "
                    "Захочешь свежее меню каждую неделю — вернуться можно в один шаг:",
    }
    return _mail_shell(heads.get(reason, heads["canceled"]),
                       bodies.get(reason, bodies["canceled"]),
                       f"{base}/quiz", "Возобновить подписку")


def _sub_canceled_email(base: str, sid: str, until: str) -> str:
    """Подтверждение отмены. Без него человек через месяц не помнит, дошло ли
    нажатие, и на всякий случай идёт блокировать карту в банк — а это уже
    чарджбек вместо спокойной отписки."""
    day = _ru_date(until)
    when = f" — до {day}" if day else ""
    return _mail_shell(
        "Подписка отменена",
        f"Больше списаний не будет. Доступ к плану и новые недели сохраняются до конца "
        f"оплаченного периода{when}. Ничего делать не нужно — подписка закончится сама.",
        f"{base}/plan/{sid}", "Открыть план",
        "<p style='color:#8A9384;font-size:13px;margin-top:18px'>Передумал — оформить подписку "
        f"снова можно в любой момент: <a href='{base}/quiz' style='color:#8A9384'>собрать план</a>.</p>")


def _sub_unbound_email(base: str, sid: str, until: str) -> str:
    """Подтверждение отвязки карты. Отвязка — не отмена: подписка ещё действует,
    и об этой разнице надо сказать прямо, иначе человек ждёт списания, которого
    не будет, или наоборот боится списания, которого тоже не будет."""
    day = _ru_date(until)
    when = f" до {day}" if day else " до конца оплаченного периода"
    return _mail_shell(
        "Карта отвязана",
        f"Автосписаний больше не будет: мы удалили сохранённый способ оплаты. "
        f"Подписка при этом продолжает работать{when} — планы приходят как обычно, "
        f"а потом она просто завершится.",
        f"{base}/plan/{sid}", "Открыть план")


def _sub_started_email(base: str, sid: str, amount, next_charge: str) -> str:
    return _mail_shell(
        "Подписка активирована",
        f"Стоимость — {amount} ₽ в месяц, следующее списание {_ru_date(next_charge)}.<br>"
        "Раз в неделю собираем новый план на 7 дней и присылаем ссылку на почту.",
        f"{base}/plan/{sid}", "Открыть план", _cancel_note(base, sid))


def _sub_renew_soon_email(base: str, sid: str, amount, next_charge: str) -> str:
    return _mail_shell(
        "Через 3 дня продлим подписку",
        f"{_ru_date(next_charge)} спишем {amount} ₽ с привязанной карты — за следующий месяц.",
        f"{base}/plan/{sid}", "Открыть план", _cancel_note(base, sid))


def _sub_charged_email(base: str, sid: str, amount, next_charge: str) -> str:
    return _mail_shell(
        "Списание по подписке",
        f"Списали {amount} ₽ за следующий месяц. Следующее списание — {_ru_date(next_charge)}.",
        f"{base}/plan/{sid}", "Открыть план", _cancel_note(base, sid))


def _plan_d2_email(base: str, token: str) -> str:
    return _mail_shell(
        "Как первый день?",
        "Отметь в плане блюда, которые уже приготовил — так видно, где ты идёшь по плану. "
        "Блюдо не подошло — замени его прямо на странице.",
        f"{base}/plan/{token}", "Открыть план")


def _plan_wait_email(base: str, link: str) -> str:
    """Оплата прошла, а план собрался урезанным (LLM был недоступен): есть меню, но нет
    рецептов и списка покупок. Письмо «Твой план на 7 дней» здесь — обещание товара,
    которого в плане нет: человек открывает и видит ровно три пустых пункта из пейволла.
    Поэтому говорим, что дособираем, а настоящее письмо уходит после дорегенерации."""
    return _mail_shell(
        "Собираем твой план",
        "Оплата прошла — спасибо. Меню на неделю уже можно посмотреть, а рецепты и список "
        "покупок дособираем: пришлём вторым письмом в течение часа, делать ничего не нужно.",
        link or f"{base}/quiz", "Посмотреть меню",
        "<p style='color:#8A9384;font-size:13px;margin-top:18px'>Если второго письма не будет "
        "через час — напиши на <a href='mailto:support@mynutriplan.ru' "
        "style='color:#8A9384'>support@mynutriplan.ru</a>, разберёмся вручную.</p>")


def _plan_d6_email(base: str, token: str, renews: bool) -> str:
    """renews — у человека действует оплаченный период, следующая неделя придёт сама.
    Без подписки ничего не обещаем: план просто остаётся по ссылке."""
    body = ("Неделя плана заканчивается. Новый план на следующие 7 дней соберём автоматически — "
            "ссылка придёт на почту." if renews else
            "Неделя плана заканчивается. Этот план остаётся по ссылке — по нему можно готовить дальше.")
    foot = "" if renews else (
        f"<p style='color:#8A9384;font-size:13px;margin-top:18px'>Нужен новый набор блюд — "
        f"<a href='{base}/quiz' style='color:#8A9384'>собрать следующий план</a>.</p>")
    return _mail_shell("Неделя заканчивается", body, f"{base}/plan/{token}", "Открыть план", foot)


_PROCESSED = DATA / "processed_payments.json"

# Замки на json-файлы, которые читают-меняют-пишут целиком. Тот же механизм, что
# у счётчиков, но здесь цена гонки другая: не сбитая статистика, а ВТОРОЕ ПИСЬМО
# и вторая строка выручки. Боевой сценарий буквальный — вебхук ЮKassa и заход
# покупателя на /pay/success приходят в одну секунду, оба зовут _already_processed,
# оба читают файл ДО записи друг друга и оба считают себя первыми.
_JSON_TLOCKS: dict[str, threading.Lock] = {}
_JSON_TLOCKS_GUARD = threading.Lock()


@contextmanager
def _json_locked(path: Path):
    """Эксклюзивный доступ к json-файлу — между потоками и между процессами."""
    DATA.mkdir(parents=True, exist_ok=True)
    with _JSON_TLOCKS_GUARD:
        tl = _JSON_TLOCKS.setdefault(path.name, threading.Lock())
    with tl:
        fh = open(path.with_suffix(path.suffix + ".lock"), "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fh.close()  # закрытие снимает flock


def _json_read(path: Path) -> dict:
    """Прочитать словарь. Вызывать только под _json_locked."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:  # noqa: BLE001
        # Битый файл идемпотентности молча возвращал {} — то есть КАЖДЫЙ вебхук
        # снова считался первым: второе письмо, второй прогон LLM, сдвиг даты
        # списания. Уводим в карантин и кричим, а не делаем вид, что всё хорошо.
        bad = path.with_name(path.stem + "." +
                             datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".corrupt")
        try:
            os.replace(path, bad)
        except OSError:
            pass
        print(f"[ALERT] {path.name} битый ({e}) — отложен в {bad.name}, начат заново", flush=True)
        return {}
    return d if isinstance(d, dict) else {}


def _json_write(path: Path, d: dict) -> None:
    """Записать словарь целиком. Вызывать только под _json_locked."""
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())      # без fsync os.replace может опередить данные
    os.replace(tmp, path)          # атомарно: читатель видит либо старый файл, либо новый


def _already_processed(pid: str) -> bool:
    """Идемпотентность вебхука: True если этот payment_id уже обрабатывали (повторная
    доставка от ЮKassa) → второй раз ничего не делаем (не сбрасываем next_charge, не шлём
    второе письмо, не дублируем LLM). Проверка и пометка — под одним замком, иначе
    двое одновременных объявляют себя первыми."""
    if not pid:
        return False
    try:
        with _json_locked(_PROCESSED):
            seen = _json_read(_PROCESSED)
            if pid in seen:
                return True
            seen[pid] = datetime.now(timezone.utc).isoformat()
            if len(seen) > 5000:  # не растим бесконечно — режем старые
                seen = dict(sorted(seen.items(), key=lambda kv: kv[1])[-3000:])
            _json_write(_PROCESSED, seen)
        return False
    except Exception as e:  # noqa: BLE001
        # Не смогли взять замок или записать. Сказать «уже обрабатывали» безопаснее,
        # чем «не обрабатывали»: недоставленный план чинят /pay/success и крон-свип,
        # а второе списание и второе письмо не чинит никто.
        print(f"[ALERT] идемпотентность недоступна ({e}) — {pid} считаем обработанным", flush=True)
        return True


_CLAIMS = DATA / "fulfill_claims.json"


def _claim(key: str, ttl_sec: int) -> bool:
    """Захватить работу под ключом: True — захватили (делаем мы), False — её уже делает
    кто-то другой. В отличие от _already_processed метка ПРОТУХАЕТ через ttl — упавшая
    (или убитая рестартом) генерация не должна блокировать повтор навсегда, иначе
    «оплачено, а плана нет» становится вечным состоянием."""
    now = datetime.now(timezone.utc)
    # Проверка аренды и её взятие — под одним замком. Без него два параллельных
    # захода читали файл до записи друг друга, оба видели «аренды нет» и оба
    # запускали генерацию: два плана, два письма, два прогона LLM.
    try:
        with _json_locked(_CLAIMS):
            d = _json_read(_CLAIMS)
            prev = d.get(key)
            if prev:
                try:
                    if (now - datetime.fromisoformat(prev)).total_seconds() < ttl_sec:
                        return False
                except Exception:  # noqa: BLE001
                    pass  # битая метка = аренды нет
            d[key] = now.isoformat()
            if len(d) > 2000:  # не растим бесконечно
                d = dict(sorted(d.items(), key=lambda kv: kv[1])[-1000:])
            _json_write(_CLAIMS, d)
        return True
    except Exception as e:  # noqa: BLE001
        # Замок недоступен. Не захватываем: пропущенная генерация чинится
        # повтором вебхука, /pay/success и крон-свипом, а вторая — ничем.
        print(f"[ALERT] аренда недоступна ({e}) — {key} не захвачен", flush=True)
        return False


def _unclaim(key: str) -> None:
    """Снять аренду досрочно: работа закончилась НЕУДАЧЕЙ, и следующий заход должен иметь
    право попробовать сразу, а не ждать протухания метки."""
    try:
        with _json_locked(_CLAIMS):
            d = _json_read(_CLAIMS)
            if d.pop(key, None) is not None:
                _json_write(_CLAIMS, d)
    except Exception:  # noqa: BLE001
        pass   # не сняли аренду — она протухнет по ttl, это и есть страховка


def _sub_mail_once(sid: str, key: str, subject: str, html: str, tag: str) -> bool:
    """Письмо по подписке ровно один раз: отметка лежит в файле подписки, поэтому
    ни повторная доставка вебхука, ни повторный тик cron дубля не дадут. Метку ставим
    ДО отправки — лучше не отправить второй раз, чем отправить дважды (сбой отправки
    и так виден в логе и счётчике mail_fail)."""
    s = _load_sub(sid)
    if not s or s.get(key):
        return False
    _sub_merge(sid, {key: datetime.now(timezone.utc).isoformat()})
    _send_email(s.get("email", ""), subject, html, tag)
    return True


def _mail_charged(sub: dict, pid: str, base: str) -> None:
    """Письмо о факте списания — одно на платёж. Об успехе одного и того же платежа
    узнают оба пути (вебхук sub_renew и досмотр pending в cron), поэтому ключ общий."""
    sid = sub.get("sub_id", "")
    if _already_processed(f"charged_mail:{pid or sid + '|' + str(sub.get('next_charge', ''))}"):
        return
    _send_email(sub.get("email", ""), "Списание по подписке · NutriPlan",
                _sub_charged_email(base, sid, sub.get("amount", SUB_PRICE_RUB),
                                   sub.get("next_charge", "")), "sub_charged")


# Окно ретраев после неудачного списания: столько cron ещё пытается взять деньги,
# потом подписка завершается. Константа общая для cron и для экрана подписки —
# в past_due человеку называют дату, до которой попытки идут, и она обязана быть
# той же самой, иначе экран снова начнёт расходиться с поведением.
SUB_GRACE = timedelta(days=3)


def _sub_until(sub: dict) -> datetime | None:
    """Конец оплаченного периода. None — даты нет или она битая."""
    try:
        d = datetime.fromisoformat(sub["next_charge"])
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _period_paid(sub: dict, now: datetime) -> bool:
    """Оплаченный период ещё не закончился (now < next_charge)."""
    end = _sub_until(sub)
    return bool(end and now < end)


def _renews_weeks(sub: dict, when: datetime) -> bool:
    """Придёт ли человеку НОВАЯ неделя плана на момент `when`.

    Один ответ на два вопроса — кого обслуживает cron и что обещать в письме 6-го дня.
    Держать их порознь нельзя: письмо обещало автосборку по «период ещё оплачен сегодня»
    и врало тем, у кого период кончается раньше следующей сборки, а past_due, которому
    недели как раз довозят, наоборот не обещало ничего."""
    st = sub.get("status")
    if st in ("active", "past_due"):  # past_due = идут ретраи списания, из обслуживания не выпал
        return True
    # Отменивший оплатил период до next_charge — недели довозим до его конца, и не дольше.
    return st == "canceled" and _period_paid(sub, when)


def _sub_view(sub: dict, now: datetime | None = None) -> dict:
    """Что показать в разделе «Подписка»: состояние, готовые тексты и доступные кнопки.

    Тексты собирает сервер, а не страница. Страница различала ровно два случая —
    active и «всё остальное» — и рисовала «Отменена · списаний больше не будет»
    любому, кто не active. Каждое из трёх остальных состояний она при этом
    описывала неверно, и по-разному опасно:
      past_due — человек НИЧЕГО не отменял, а cron в grace-окне как раз пытается
                 списать; экран обещал, что списаний не будет, и это прямая ложь
                 о деньгах;
      период истёк — тот же текст «доступ до конца периода» с датой в прошлом и
                 без единого способа вернуться;
      ended    — то же самое, хотя подписка уже закончилась.

    Состояние считаем по статусу И по дате: статус canceled с истёкшим периодом —
    это уже «завершена», даже если cron ещё не успел переставить поле.
    """
    now = now or datetime.now(timezone.utc)
    st = (sub or {}).get("status") or ""
    end = _sub_until(sub or {})
    until = end.isoformat() if end else ""
    until_ru = _ru_date(until) if until else ""
    amount = (sub or {}).get("amount", SUB_PRICE_RUB)
    # Решает payment_method_id, а `has_card` — только когда поля нет вовсе (старые
    # записи). Через ИЛИ нельзя: у демо-подписки has_card=True зашит навсегда, и
    # после «Отвязать карту» экран продолжал обещать списание с несуществующей карты.
    _pm = (sub or {}).get("payment_method_id")
    has_card = bool(_pm) if _pm is not None else bool((sub or {}).get("has_card"))
    over = bool(end and now >= end)
    v = {
        "status": st,                 # сырой статус — совместимость со старой вёрсткой
        "next": until,                # то же поле, что отдавали раньше
        "amount": amount,
        "has_card": has_card,
        "until": until,
        "until_ru": until_ru,
        # С лендингом подписки: голый /quiz воспринимался как заход с другого
        # лендинга и стирал сохранённые ответы.
        "resume_url": ("/quiz?l=" + "".join(c for c in str((sub or {}).get("landing") or "")
                                            if c.isalnum() or c in "-_")[:24]
                       if (sub or {}).get("landing") else "/quiz"),
    }
    if st == "active" and has_card:
        v.update(state="active", badge="Активна",
                 line=f"Следующее списание: {until_ru} · {amount} ₽/мес" if until_ru
                      else f"{amount} ₽/мес",
                 note="Карта привязана для автопродления",
                 can_cancel=True, can_unbind=True, can_resume=False)
    elif st == "active":
        # Карта отвязана, но период оплачен: списаний не будет, доступ идёт.
        v.update(state="active", badge="Активна",
                 line=f"Автопродления не будет · доступ до {until_ru}" if until_ru
                      else "Автопродления не будет",
                 note="Карта не привязана — автосписаний не будет",
                 can_cancel=True, can_unbind=False, can_resume=False)
    elif st == "past_due" and not (end and now > end + SUB_GRACE):
        # Не «Отменена»: человек ничего не отменял, деньги просто не прошли.
        # Окно ретраев истекло — идём в ветку «Завершена» ниже: cron в этом случае
        # больше не пытается списать, и обещать попытку до вчерашней даты нельзя.
        retry_ru = _ru_date((end + SUB_GRACE).isoformat()) if end else ""
        v.update(state="past_due", badge="Платёж не прошёл",
                 line=f"Не удалось списать {amount} ₽ — автопродление остановлено",
                 note=(f"Пробуем ещё раз до {retry_ru}. Подписка не отменена, доступ пока сохраняется."
                       if retry_ru else
                       "Пробуем списать ещё раз. Подписка не отменена, доступ пока сохраняется."),
                 # Отмена и отвязка обязаны работать именно здесь: это единственный
                 # способ остановить ретраи, и прятать их значит толкать человека в банк.
                 can_cancel=True, can_unbind=has_card, can_resume=False)
    elif st == "canceled" and not over:
        v.update(state="canceled", badge="Отменена",
                 line=f"Доступ до {until_ru}" if until_ru else "Доступ до конца оплаченного периода",
                 note="Списаний больше не будет. До этой даты новые недели плана продолжают приходить.",
                 can_cancel=False, can_unbind=has_card, can_resume=True)
    else:
        # ended, а также canceled/active с истёкшим периодом — фактически то же самое.
        v.update(state="ended", badge="Завершена",
                 line=f"Подписка завершена {until_ru}" if until_ru else "Подписка завершена",
                 note="Новые недели больше не приходят. Последний план остаётся по этой ссылке.",
                 can_cancel=False, can_unbind=False, can_resume=True)
    return v


def _plan_email(token: str) -> str:
    """Почта владельца плана: сначала подписка, иначе журнал заказов (разовая покупка)."""
    sub = _load_sub(token)
    return (sub.get("email") or "").strip() or (_find_order(token).get("email") or "").strip()


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


def _sub_by_plan_token(token: str) -> str:
    """sub_id подписки, которой принадлежит план. Обычно sub_id == plan_token (так и создаём),
    но полагаться только на это нельзя — иначе настройки плана не доехали бы до подписки."""
    safe = "".join(c for c in (token or "") if c.isalnum())
    if not safe:
        return ""
    if _load_sub(safe):
        return safe
    for s in _all_subs():
        if s.get("plan_token") == safe:
            return s.get("sub_id", "")
    return ""


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


def _order_by_payment(pid: str) -> dict:
    """Найти заказ по payment_id ЮKassa.

    Нужно для возвратов: в уведомлении о возврате нашей метаданной с order id нет,
    там только payment_id. Идём по журналу с конца — последняя запись про этот
    платёж самая свежая."""
    if not pid:
        return {}
    try:
        lines = ORDERS.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return {}
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue  # битая строка не должна обрывать поиск
        if rec.get("payment_id") == pid:
            return rec
    return {}


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
#
# Схема «прочитал → прибавил → перезаписал файл целиком» без лока и без atomic
# replace разваливалась на любом всплеске трафика: 60 параллельных заходов
# доезжали до диска как 11 (потеря 82%), а на 64 файл с первой же попытки
# превращался в огрызок `{"visit_slim": 3} "visit_slim_organic": 1}`. Дальше
# было хуже: чтение и запись стояли в одном try/except: pass, поэтому после
# порчи КАЖДЫЙ _bump падал на чтении и запись не выполнялась — визиты, лиды,
# pay_ok, sub_ok и возвраты не считались больше никогда, /admin/stats отдавал
# нули, и ни одной строки в логе об этом не было.
#
# Отсюда три требования, а не одно: (1) лок — иначе инкременты затирают друг
# друга; (2) tmp + os.replace — иначе на диске оказывается полуфайл; (3) битый
# файл не глотать — уводить в .corrupt и кричать, иначе он молча блокирует счёт
# до конца жизни сервера.
_COUNTERS_TLOCK = threading.Lock()      # потоки одного процесса: sync-роуты живут в threadpool
COUNTERS_LOCK = DATA / "counters.lock"  # процессы: несколько воркеров uvicorn + cron-скрипт


@contextmanager
def _counters_locked():
    """Эксклюзивный доступ к counters.json — и между потоками, и между процессами."""
    DATA.mkdir(parents=True, exist_ok=True)
    with _COUNTERS_TLOCK:
        fh = open(COUNTERS_LOCK, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fh.close()  # закрытие снимает flock


def _counters_read() -> dict:
    """Прочитать счётчики. Вызывать только под _counters_locked."""
    try:
        raw = COUNTERS.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:  # noqa: BLE001
        print(f"[ALERT] counters: файл не читается ({e}) — счёт продолжится с нуля", flush=True)
        return {}
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except ValueError as e:  # битьё, оставшееся от старой схемы записи
        bad = COUNTERS.with_name(
            "counters." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".corrupt")
        try:
            os.replace(COUNTERS, bad)
        except OSError:
            pass
        print(f"[ALERT] counters.json битый ({e}) — отложен в {bad.name}, счёт начат заново",
              flush=True)
        return {}
    return d if isinstance(d, dict) else {}


def _counters_write(d: dict) -> None:
    """Записать счётчики целиком. Вызывать только под _counters_locked."""
    tmp = COUNTERS.with_name(f"counters.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())      # без fsync os.replace может опередить данные
    os.replace(tmp, COUNTERS)      # атомарно: читатель видит либо старый файл, либо новый


def _bump(name: str, n: int = 1) -> None:
    if notrack():
        return
    try:
        with _counters_locked():
            d = _counters_read()
            try:
                cur = int(d.get(name, 0) or 0)
            except (TypeError, ValueError):
                cur = 0
            d[name] = cur + n
            _counters_write(d)
    except Exception as e:  # noqa: BLE001
        # Молчать нельзя: потерянный счётчик — это потерянная строка в /admin/stats,
        # по которой владелец решает, куда лить деньги.
        print(f"[ALERT] counters: не учтён {name!r}: {e}", flush=True)


def _counters() -> dict:
    try:
        with _counters_locked():
            return _counters_read()
    except Exception as e:  # noqa: BLE001
        print(f"[ALERT] counters: не прочитаны ({e})", flush=True)
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


# Адреса наших обратных прокси (nginx перед контейнером). Пусто по умолчанию —
# X-Forwarded-For присылает КЛИЕНТ, и пока мы верили ему на слово, каждый лимит
# «N в час на IP» снимался одной строкой в заголовке: замерено — 18 запросов без
# заголовка отсекались на 15-м, те же 20 с подставным XFF проходили все 20.
# Кому верим в X-Forwarded-For. Переменная переопределяет умолчание:
# NUTRI_TRUSTED_PROXIES=10.0.0.5 — доверять только этому адресу.
#
# Умолчание — приватные и loopback-адреса, и это безопасно по устройству сети:
# доверяем не заголовку, а ПИРУ TCP-соединения. Если перед нами стоит прокси, пир
# это он (loopback или адрес docker-сети). Если прокси нет, пир — сам посетитель
# из интернета с публичным адресом, и его заголовок мы игнорируем. Подделать пир
# нельзя: это адрес сокета, а не строка в запросе.
#
# Пустое умолчание было хуже обоих вариантов: переменная не задана нигде в
# репозитории, а прод стоит за прокси — значит все посетители схлопывались в
# один адрес прокси, и лимит «20 лидов в час» становился общим на весь сайт.
TRUSTED_PROXIES = {p.strip() for p in os.getenv("NUTRI_TRUSTED_PROXIES", "").split(",") if p.strip()}


def _peer_trusted(peer: str) -> bool:
    """Доверять адресу можно ТОЛЬКО по явному списку.

    Умолчание «доверяем приватным сетям» я уже пробовал — и оно оказалось хуже
    пустого: при публикации порта докером пиром выглядит шлюз 192.168.65.1, то
    есть приватным становится КАЖДЫЙ клиент, и подделка заголовка снова проходит.
    Замерено в контейнере из этого же Dockerfile: X-Forwarded-For: 9.9.9.9
    ложился в журнал согласий.

    Пустой список означает «заголовку не верим никому» — это безопасно, но за
    прокси все посетители сливаются в один адрес. Поэтому ниже стоит громкий
    однократный ALERT: он называет адрес, который надо вписать в переменную.
    """
    return bool(TRUSTED_PROXIES) and peer in TRUSTED_PROXIES


_XFF_WARNED = False


def _client_ip(request: Request) -> str:
    """IP, по которому считаем лимиты и пишем в журнал согласий.

    Работает в паре с --no-proxy-headers в Dockerfile. Своя мидлварь uvicorn для
    этого не годится: она берёт из цепочки ЛЕВЫЙ элемент, а левый подставляет
    клиент — nginx с $proxy_add_x_forwarded_for дописывает настоящий адрес
    СПРАВА, к присланному. Замерено: с включённой мидлварью подставной 9.9.9.9
    попадает в журнал согласий, сколько ни правь этот файл.

    Поэтому: заголовок читаем сами, только от пира из NUTRI_TRUSTED_PROXIES, и
    берём ПРАВЫЙ элемент — его дописал наш прокси, всё левее мог сочинить клиент.
    """
    peer = (request.client.host if request.client else "") or "?"
    xff = request.headers.get("x-forwarded-for", "")
    if xff and _peer_trusted(peer):
        return xff.split(",")[-1].strip() or peer
    global _XFF_WARNED
    if xff and not _XFF_WARNED and not _peer_trusted(peer):
        # Кричим один раз. Если прод стоит за прокси, а список пуст, все клиенты
        # схлопнулись в один IP и лимиты режут живых людей. Узнать об этом лучше
        # из лога, чем из жалоб «не приходит письмо». Адрес прокси — вот он.
        _XFF_WARNED = True
        print(f"[ALERT] X-Forwarded-For пришёл от {peer}, а его нет в "
              f"NUTRI_TRUSTED_PROXIES — заголовок игнорируем, все посетители "
              f"считаются одним IP. Если {peer} это наш прокси, добавь его в "
              f"переменную", flush=True)
    return peer


# ── Согласие на обработку ПДн ────────────────────────────────────────────────
# Вариант B доктрины golive-legal: не чекбокс, а подпись прямо над кнопкой.
# 152-ФЗ формы не предписывает — ст. 9 требует, чтобы согласие было конкретным,
# информированным и однозначным, и чтобы оператор МОГ ЕГО ПОДТВЕРДИТЬ. Поэтому
# подпись допустима только вместе с полной фиксацией ниже.
#
# Текст версионирован: меняешь формулировку — поднимаешь версию, иначе старые
# записи будут утверждать, что человек согласился с текстом, которого не видел.
# v2: в Согласие и Политику добавлен OpenRouter (США) — анкета уходит модели, и
# это трансграничная передача. Подпись у кнопки не изменилась ни на символ, но
# версия обязана: она отвечает на вопрос «с ЧЕМ человек согласился», а согласие
# ссылается на документы. Без бампа записи июля утверждали бы, что люди согласились
# на передачу за границу, которой в тексте на тот момент не было.
CONSENT_VERSION = "v2-2026-07-31"
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


def _record_consent(request: Request, email: str, action: str, method: str = "button_click",
                    text: str = "", version: str = "") -> None:
    """Записать факт согласия так, чтобы он что-то доказывал через год.

    `consent: true` + время не доказывают ничего: московское УФАС однажды не
    приняло галочку как доказательство, потому что нельзя было показать, КТО
    и НА ЧТО согласился. Поэтому десять полей, а не два.

    Текст и версия — ВСЕГДА серверные. Раньше писали присланные клиентом, и
    журнал получался самоопровергающимся: посторонний слал POST с чужим адресом
    и произвольным consent_text, а в consents.jsonl ложилось «этот человек
    согласился» с текстом, которого мы никогда не показывали. Присланное
    оставляем рядом как диагностику: расхождение значит, что где-то живёт старый
    фронт со старой формулировкой, и это надо видеть.

    `action` — на каком действии получено согласие (quiz_lead, pay, subscribe,
    login_link, …). Без него журнал не отвечает на вопрос «в связи с чем»,
    а именно он определяет объём обрабатываемых данных.
    """
    seen = (text or "").strip()
    ver = (version or "").strip()
    rec = {
        "at": datetime.now(timezone.utc).isoformat(),
        "email": (email or "").strip().lower(),
        "action": action,                 # точка, где человек передал нам ПДн
        "method": method,                 # button_click | checkbox
        "text": CONSENT_TEXT,             # то, что мы показываем на этой кнопке
        "version": CONSENT_VERSION,
        # Клиент прислал НЕ ту формулировку, что у нас сейчас, — сигнал о старом фронте.
        "stale_wording": bool(ver and ver != CONSENT_VERSION) or bool(seen and seen != CONSENT_TEXT),
        "client_version": ver,
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
# Неделя, а не месяц: по этому токену БЕЗ всякой авторизации отдаются ПДн лида —
# почта, рост, вес, цель. Письмо со ссылкой читают в первые дни, а жил токен 30 —
# то есть месяц любой, кому попал чужой почтовый ящик или пересланное письмо,
# доставал профиль здоровья.
LEAD_TOKEN_TTL = 7 * 86400
# Токен из АДРЕСА убираем на входе (см. quiz): /quiz?resume=<токен> уходит в Метрику
# целиком — и в page-url хита, и в page-ref у целей. Настоящий токен кладём в куку,
# в адресе остаётся метка-заглушка, по ней квиз просит те же данные.
LEAD_RESUME_COOKIE = "np_resume"
LEAD_RESUME_MARK = "1"          # что видно в адресе вместо токена
LEAD_RESUME_COOKIE_AGE = 1800   # кука нужна на одну загрузку квиза, дольше не живёт


def _lead_token_make(email: str, quiz: dict) -> str:
    import uuid
    tok = uuid.uuid4().hex
    now = datetime.now(timezone.utc).timestamp()
    try:
        d = json.loads(LEAD_TOKENS.read_text()) if LEAD_TOKENS.exists() else {}
    except Exception:  # noqa: BLE001
        d = {}
    # Чистим протухшие и заодно подрезаем выданные под старый месячный срок:
    # иначе сокращение TTL не касалось бы уже разосланных писем ещё месяц.
    d = {k: {**v, "exp": min(v.get("exp", 0), now + LEAD_TOKEN_TTL)}
         for k, v in d.items() if v.get("exp", 0) > now}
    d[tok] = {"email": email, "quiz": quiz, "exp": now + LEAD_TOKEN_TTL}
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


# ── Метки рекламного клика ───────────────────────────────────────────────────
# Человек приходит по объявлению на /l/slim?utm_source=yandex&…&yclid=…, а на
# квиз и в оплату метки не доезжали — SRC становился 'organic', и КАЖДАЯ платная
# покупка ложилась в orders.jsonl как organic. Разрез «сколько денег принёс
# источник» был физически невозможен.
#
# Поэтому метки запоминаются в куке на входе (любой маршрут, см. _notrack_mw) и
# кладутся СЫРЫМИ в лид и в заказ. Кука — 90 дней: столько живёт окно атрибуции
# Яндекс.Директа, покупка через месяц после клика должна остаться за источником.
UTM_COOKIE = "np_utm"
UTM_COOKIE_AGE = 60 * 60 * 24 * 90
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "yclid", "gclid")


def _marks_from_query(request: Request) -> dict:
    q = request.query_params
    out = {}
    for k in UTM_KEYS:
        v = (q.get(k) or "").strip()[:200]
        if v:
            out[k] = v
    return out


def _marks_from_cookie(request: Request) -> dict:
    raw = request.cookies.get(UTM_COOKIE, "")
    if not raw:
        return {}
    try:
        d = json.loads(unquote(raw))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(d, dict):
        return {}
    return {k: str(v)[:200] for k, v in d.items() if k in UTM_KEYS and v}


def _marks(request: Request) -> dict:
    """Метки текущего клика, иначе — запомненные с прошлого захода."""
    return _marks_from_query(request) or _marks_from_cookie(request)


def _remember_marks(request: Request, response) -> None:
    """Положить метки в куку. Перезаписываем последним кликом, а не первым:
    yclid обязан быть от ТОГО клика, по которому пришли деньги, иначе офлайн-
    конверсия уедет не на то объявление."""
    marks = _marks_from_query(request)
    if not marks:
        return
    try:
        response.set_cookie(UTM_COOKIE, quote(json.dumps(marks, ensure_ascii=False)),
                            max_age=UTM_COOKIE_AGE, samesite="lax", path="/", httponly=True)
    except Exception:  # noqa: BLE001
        pass  # метка полезна, но ронять из-за неё ответ страницы нельзя


def _ad_ids(request: Request, marks: dict) -> dict:
    """Идентификаторы для ОФЛАЙН-конверсии: ClientId Метрики (кука _ym_uid, её ставит
    счётчик на нашем же домене) и yclid из меток клика.

    Цель pay_success шлётся только со страницы возврата и только при мгновенном
    succeeded. Закрыл вкладку в приложении банка, вернулся при pending, API молчал —
    деньги пришли, план выдан, а Метрика и Директ конверсии не увидели. Догрузить её
    офлайном можно только по ClientId или yclid, и раньше их не оставалось нигде:
    заказ помнил utm, но не помнил, КОМУ в Метрике эта покупка принадлежит.

    Пишем в заказ при СОЗДАНИИ платежа: на вебхуке ни куки, ни меток уже нет."""
    out = {}
    cid = (request.cookies.get("_ym_uid") or "").strip()[:32]
    if cid.isdigit():  # ClientId Метрики — всегда число; мусор из чужой куки не берём
        out["ym_uid"] = cid
    if marks.get("yclid"):
        out["yclid"] = marks["yclid"]  # дублируем из utm наверх: выгрузку строят по нему
    return out


def _src_of(q: dict) -> str:
    if q.get("yclid") or q.get("gclid"):
        return "ad"
    if (q.get("utm_medium") or "").lower() in ("cpc", "ppc", "paid"):
        return "ad"
    if (q.get("utm_source") or "").lower() in ("yandex", "direct"):
        return "ad"
    return "organic"


def _detect_src(request: Request) -> str:
    """Источник ТЕКУЩЕГО захода — только по адресу. Для счётчика визитов куку
    брать нельзя: человек, кликнувший объявление месяц назад и пришедший теперь
    по прямой ссылке, — это органический визит, а не второй платный."""
    return _src_of(_marks_from_query(request))


def _attr_src(request: Request) -> str:
    """Источник для АТРИБУЦИИ ДЕНЕГ: адрес, а если меток в нём нет — кука.
    До /api/pay/create метки в адресе не доходят никогда (это POST со страницы
    квиза), поэтому без куки любая платная покупка оказывалась organic."""
    return _src_of(_marks(request))


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


def _inject_metrika(html: str, anon_page: str = "") -> str:
    """Вставить счётчик Я.Метрики перед </head>, ЕСЛИ задан NUTRI_METRIKA_ID (инфра готова —
    оператору достаточно задать env, код появится на всех лендингах/квизе/плане автоматически).

    anon_page — обезличенный адрес хита для страниц, чей АДРЕС СЕКРЕТЕН (в нём
    лежит токен плана). Тогда: defer:true — автоматический хит с настоящим
    адресом не уходит, вместо него один обезличенный с пустым referer;
    clickmap/trackLinks выключены — они шлют page-ref, а это тот же адрес.

    ВАЖНО про границы приёма: одного anon_page МАЛО. Перехват сетевых запросов
    показал, что при defer:true Метрика всё равно шлёт технический
    `watch/<id>?page-url=<настоящий адрес>&nohit=1`. Поэтому там, где адрес
    секретен, счётчик либо не ставится вовсе (/plan/{token}), либо адрес
    предварительно вычищается из location через history.replaceState
    (/pay/success), и только тогда anon_page работает как задумано.
    """
    if notrack() or not (NUTRI_METRIKA_ID and "</head>" in html):
        return html
    cid = NUTRI_METRIKA_ID
    if anon_page:
        init = (f"ym({cid},'init',{{defer:true,clickmap:false,trackLinks:false,"
                f"accurateTrackBounce:true,webvisor:false}});"
                f"ym({cid},'hit','{anon_page}',{{referer:''}});")
        pixel = ""  # noscript-пиксель уходит с Referer страницы — на секретном адресе не нужен
    else:
        init = f"ym({cid},'init',{{clickmap:true,trackLinks:true,accurateTrackBounce:true,webvisor:false}});"
        pixel = (f"<noscript><div><img src='https://mc.yandex.ru/watch/{cid}' "
                 f"style='position:absolute;left:-9999px' alt='' /></div></noscript>")
    snippet = (
        "<script type='text/javascript'>(function(m,e,t,r,i,k,a){m[i]=m[i]||function(){"
        "(m[i].a=m[i].a||[]).push(arguments)};m[i].l=1*new Date();"
        "for(var j=0;j<document.scripts.length;j++){if(document.scripts[j].src===r){return;}}"
        "k=e.createElement(t),a=e.getElementsByTagName(t)[0],k.async=1,k.src=r,a.parentNode.insertBefore(k,a)})"
        "(window,document,'script','https://mc.yandex.ru/metrika/tag.js','ym');"
        + init +
        # Идентификатор наружу: любая страница шлёт цели через window.npGoal(),
        # не зная номера счётчика и не ломаясь, когда счётчик не настроен.
        f"window.NP_METRIKA_ID={cid};"
        "window.npGoal=function(n){try{if(window.ym&&window.NP_METRIKA_ID)"
        "ym(window.NP_METRIKA_ID,'reachGoal',n);}catch(e){}};</script>" + pixel)
    return html.replace("</head>", snippet + "</head>", 1)


def _no_referrer_leak(html: str) -> str:
    """Запретить браузеру класть адрес страницы в Referer чужих запросов.

    Вторая половина той же дыры: даже без счётчика любой сторонний ресурс на
    странице (пиксель, шрифт, ссылка наружу) получил бы токен в заголовке
    Referer. Ставим ДО счётчика и независимо от него — политика должна работать
    и когда NUTRI_METRIKA_ID не задан."""
    if "</head>" not in html or "name='referrer'" in html or 'name="referrer"' in html:
        return html
    return html.replace("</head>", "<meta name='referrer' content='same-origin'></head>", 1)


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
# Индекс слагов вместо поиска по одному. Раньше запоминались только ПОПАДАНИЯ,
# поэтому каждый запрос по неизвестному слагу перечитывал ВЕСЬ каталог планов:
# замер на 300 планах — 28 мс против 0,9 мс у /api/health, а под 40 параллельными
# промахами /api/health отвечал 1,2 с. Ручка открыта наружу, слаг в адресе
# произвольный — то есть полный обход диска заказывал кто угодно и сколько угодно
# раз, и цена росла вместе с числом клиентов.
#
# Теперь обход планов — ОДИН на все промахи: он собирает слаги всех планов сразу,
# после чего любой промах отвечает из памяти. Повторяется не чаще раза в TTL и
# только если промах вообще случился. Размер набора ограничен каталогом блюд
# (промпт заставляет модель брать названия оттуда), а посторонний в него не
# пишет — слаг из адреса туда не попадает.
_PLAN_INDEX_AT = 0.0            # когда индекс последний раз строился обходом планов
PLAN_INDEX_TTL = 600
_PLAN_INDEX_LOCK = threading.Lock()


def _known_slugs() -> set[str]:
    """Слаги каталога dishes.json — читаются один раз за жизнь процесса."""
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
    return _KNOWN_SLUGS


def _index_dish_slugs(pl: dict) -> None:
    """Занести блюда сохранённого плана в индекс известных слагов.

    Индекс пополняется ПРИ ЗАПИСИ плана, а не только обходом: иначе фото свежей
    замены ждало бы следующего обхода (до 10 минут с пустой тарелкой на экране)."""
    try:
        known = _known_slugs()
        for d in pl.get("days") or []:
            for m in d.get("meals") or []:
                s = dish_photos.slugify(m.get("name", ""))
                if s:
                    known.add(s)
    except Exception:  # noqa: BLE001
        pass  # индекс — ускорение, а не условие сохранения плана


def _reindex_plans() -> None:
    """Собрать слаги ВСЕХ сохранённых планов за один обход.

    Нужен только для планов, записанных до старта процесса (свои пишет
    _index_dish_slugs). Под замком: сорок одновременных промахов должны стоить
    один обход, а не сорок."""
    global _PLAN_INDEX_AT
    with _PLAN_INDEX_LOCK:
        now = datetime.now(timezone.utc).timestamp()
        if now - _PLAN_INDEX_AT < PLAN_INDEX_TTL:
            return  # пока ждали замок, обход сделал кто-то другой
        try:
            for f in PLANS.glob("*.json"):
                try:
                    _index_dish_slugs(json.loads(f.read_text(encoding="utf-8")))
                except Exception:  # noqa: BLE001
                    continue  # битый файл плана не должен отменять весь индекс
        except Exception:  # noqa: BLE001
            pass
        _PLAN_INDEX_AT = now


def _dish_known_cached(slug: str) -> bool | None:
    """Ответ без чтения диска: True/False — знаем, None — нужен обход планов."""
    if slug in _known_slugs():
        return True
    if datetime.now(timezone.utc).timestamp() - _PLAN_INDEX_AT < PLAN_INDEX_TTL:
        return False  # индекс свежий: нет в нём — значит такого блюда нет
    return None


def _is_known_dish(slug: str) -> bool:
    """Слаг из каталога dishes.json или из любого сохранённого плана — только для таких
    разрешаем платную генерацию фото (защита от амплификации через /dish/произвольное)."""
    cached = _dish_known_cached(slug)
    if cached is not None:
        return cached
    # блюдо из свежесгенерированного плана (замены) могло не быть в каталоге — проверим планы
    _reindex_plans()
    return slug in _known_slugs()


@app.api_route("/dish/{slug}", methods=["GET", "HEAD"])
def dish_photo(slug: str, bg: BackgroundTasks, request: Request, t: str = "", lg: str = "") -> Response:
    """Фото блюда из общего кэша; если нет — плейсхолдер + фоновая генерация.
    Генерируем ТОЛЬКО для известных блюд (каталог + сохранённые планы) — иначе любой мог бы
    заказывать платную LLM-генерацию произвольных слагов (финансовый DoS + переполнение диска)."""
    slug = "".join(c for c in slug if c.isalnum() or c == "-")[:60]
    if slug and dish_photos.has_photo(slug):
        # lg=1 — просмотр на весь экран: отдаём крупный вариант, если он есть.
        # В списке марка 64px, и тянуть туда крупный файл незачем.
        return FileResponse(str(dish_photos.photo_file(slug, big=lg == "1")), media_type="image/webp",
                            headers={"Cache-Control": "public, max-age=2592000, immutable"})
    known = _dish_known_cached(slug) if slug else False
    if known is None:
        # Обход всех планов — единственная дорогая ветка ручки, и заказывает её
        # ЧУЖОЙ слаг. Лимит по IP держит её на посторонних: у своих блюда лежат
        # в кэше (каталог + _write_plan), сюда они не попадают вовсе.
        if _rate_ok("dish_scan", _client_ip(request), 60, 3600):
            known = _is_known_dish(slug)
        else:
            _bump("dish_scan_limited")
            known = False
    if known:
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
def quiz(request: Request, l: str = DEFAULT_LANDING, resume: str = "") -> Response:
    """Единый квиз, темизированный под лендинг (?l=slug).

    ?resume=<токен> из письма-лида до страницы не доходит: сначала 302 на тот же
    адрес с меткой вместо токена, сам токен — в куке. Причина в Метрике: она шлёт
    адрес страницы в page-url хита, а на КАЖДОЙ цели ещё и в page-ref, и по этому
    токену GET /api/lead/resume отдаёт почту, рост, вес и цель. Перехват запросов
    показал четыре запроса на mc.yandex с токеном внутри на одной загрузке квиза.
    Вычистить адрес скриптом, как на /pay/success, здесь нельзя: квиз читает
    resume из location, а порядок «вычистили → прочитали» не гарантирован.
    """
    slug = l if l in LANDINGS else DEFAULT_LANDING
    tok = "".join(c for c in (resume or "") if c.isalnum())[:64]
    if tok and tok != LEAD_RESUME_MARK:
        q = {k: v for k, v in request.query_params.items() if k != "resume"}
        q["resume"] = LEAD_RESUME_MARK
        r: Response = RedirectResponse(f"/quiz?{urlencode(q)}", status_code=302)
        r.set_cookie(LEAD_RESUME_COOKIE, tok, max_age=LEAD_RESUME_COOKIE_AGE,
                     samesite="lax", httponly=True, path="/")
        return r
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
                .replace("__SRC__", _attr_src(request))  # на квиз метки могли не доехать — берём и из куки
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


# ── Квиз: белый список полей ─────────────────────────────────────────────────
# quiz приходил на сервер как есть — любой словарь любого размера ложился в
# leads.jsonl и в orders.jsonl. Это и мусор в данных, по которым потом собирают
# план, и свободный канал «пиши что хочешь ко мне на диск».
#
# Форма каждого поля фиксирована: строка / список строк / три числа тела. Всё
# незнакомое отбрасывается, а факт отбрасывания виден в счётчике — если фронт
# заведёт новое поле, оно не пропадёт молча.
_QUIZ_FIELDS = {
    "goal": "str", "gender": "str", "age": "str", "activity": "str",
    "meals": "str", "cook": "str", "exclude": "str",
    "why": "list", "barriers": "list", "diet": "list",
    "favorites": "list", "disliked": "list",
    "body": "body",
}


def _clean_quiz(quiz) -> dict:
    if not isinstance(quiz, dict):
        return {}
    out: dict = {}
    for k, kind in _QUIZ_FIELDS.items():
        if k not in quiz:
            continue
        v = quiz[k]
        if kind == "str":
            if isinstance(v, (str, int, float)):
                s = str(v).strip()[:300]   # exclude («что ещё не ем») — самое длинное поле
                if s:
                    out[k] = s
        elif kind == "list":
            if isinstance(v, list):
                vals = [str(x).strip()[:80] for x in v[:40]
                        if isinstance(x, (str, int, float)) and str(x).strip()]
                if vals:
                    out[k] = vals
        elif kind == "body" and isinstance(v, dict):
            body = {}
            for f in ("height", "weight", "target"):
                try:
                    n = float(v.get(f))
                except (TypeError, ValueError):
                    continue
                if 0 < n < 500:            # рост/вес человека, а не произвольное число
                    body[f] = round(n, 1)
            if body:
                out["body"] = body
    if len(quiz) > len(out):
        _bump("quiz_extra_keys")           # фронт прислал что-то сверх списка — надо посмотреть
    return out


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
        # Ниже — «без мяса + без лактозы», в том числе с «без глютена».
        # Каталог для этой комбинации был почти пуст: оставались тосты, овсянка
        # на воде и фруктовый салат, то есть завтрак без белка — для плана
        # похудения это прямо плохо. Поэтому в каталог добавлены веганские
        # белковые блюда, и здесь они идут ПЕРЕД углеводными: первый подошедший
        # выигрывает, значит порядок и есть приоритет.
        "Тофу-скрэмбл с овощами": "Тофу, помидоры, шпинат",
        "Нут с овощами и зеленью": "Нут, огурцы, зелень",
        "Каша киноа на растительном молоке": "Киноа, растительное молоко, ягоды",
        "Чиа-пудинг на кокосовом молоке": "Семена чиа, кокосовое молоко, фрукты",
        "Гречневая каша с семенами": "Гречка, тыквенные семечки, ягоды",
        "Смузи-боул с семенами и ягодами": "Ягоды, банан, семена",
        "Тосты с авокадо": "Хлеб, авокадо, лимон",
        "Овсянка на воде": "Овсянка, корица, фрукты",
        # Самый последний резерв. Белка тут нет, но провалиться в запасную ветку
        # и показать блюдо без состава — хуже.
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
    import plan_ai
    quiz = req.quiz or {}
    kc = plan.compute(quiz)["cal"]
    # Приёмы берём из ответа квиза, а не из трёх захардкоженных строк. Экран
    # оплаты обещал «5 приёмов пищи в день» тому, кто выбрал «3 + перекусы», и
    # тут же показывал завтрак, обед и ужин — сам себя опровергал. Таблица одна
    # и та же для примера дня, для банк-заготовки и для проверки ответа модели
    # (plan_ai.MEAL_LAYOUT), иначе они снова разъедутся.
    layout = plan_ai.meal_layout(quiz)
    split = [round(kc * share) for _, _, share in layout]

    meals = []
    excl_terms = _preview_excluded(quiz)
    excl_flags = _preview_flags(quiz)
    try:
        allowed = plan_ai._allowed_by_meal(quiz)
        by_title = {d["title"]: d for d in plan_ai._catalog()}
        seen: dict[str, int] = {}
        for i, (label, key, _share) in enumerate(layout):
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
                    # По флагу meat, а не по «не veg»: иначе яичные завтраки
                    # считались мясом и пропадали у тех, кто мясо не ест.
                    if "meat" in excl_flags and d.get("meat"):
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
            # Один раздел каталога может встретиться в дне дважды (два перекуса) —
            # второй берёт следующее блюдо, иначе оба перекуса одинаковые.
            n = seen.get(key, 0)
            seen[key] = n + 1
            title = pool[n % len(pool)]
            d = by_title.get(title, {})
            meals.append({"meal": label, "title": title, "kcal": split[i],
                          "desc": PREVIEW_PICKS.get(key, {}).get(title, ""),
                          "slug": d.get("slug", ""),
                          "photo": bool(d.get("slug")) and dish_photos.has_photo(d["slug"])})
    except Exception:  # noqa: BLE001
        meals = []

    if not meals:
        # Каталог не дал ни одного блюда под эти ограничения — отдаём запасной
        # день из банка. Он беднее, зато существует всегда. Названия берём из
        # банка по кругу, но приёмы и калории — из той же раскладки: запасной
        # путь не должен опровергать обещание про число приёмов.
        try:
            names = [t for _, t, _ in plan.week(quiz)[0]["meals"]]
            meals = [{"meal": label, "title": names[i % len(names)], "kcal": split[i], "desc": "",
                      "slug": dish_photos.slugify(names[i % len(names)]),
                      "photo": dish_photos.has_photo(dish_photos.slugify(names[i % len(names)]))}
                     for i, (label, _key, _share) in enumerate(layout)]
        except Exception:  # noqa: BLE001
            return JSONResponse({"meals": []})

    return JSONResponse({"meals": meals})


# Предохранитель домена: сколько писем-лидов вообще может уйти за сутки со всего
# сайта. Лимиты по адресу и по IP не спасают от распределённой заливки, а цена
# здесь не «спам», а репутация домена в Unisender Go: сгорит она — письма
# перестанут доходить ОПЛАТИВШИМ. Живому бизнесу потолок не мешает: лидов у нас
# единицы-десятки в сутки, до 200 не дотягивает даже удачный день.
LEAD_MAIL_DAILY_CAP = int(os.getenv("NUTRI_LEAD_MAIL_DAILY_CAP", "200"))


def _lead_mail_budget_ok() -> bool:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"lead_mail_{day}"
    if int(_counters().get(key, 0) or 0) >= LEAD_MAIL_DAILY_CAP:
        _bump("lead_mail_capped")
        print(f"[ALERT] суточный потолок писем-лидов ({LEAD_MAIL_DAILY_CAP}) исчерпан — "
              f"письмо не отправлено, лид сохранён", flush=True)
        return False
    _bump(key)
    return True


@app.post("/api/lead")
def save_lead(lead: Lead, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Лид с квиза: строка в leads.jsonl + письмо с нормой и ссылкой на пейволл.

    Ручка открыта наружу и на КАЖДЫЙ запрос шлёт письмо с нашего домена на любой
    указанный адрес — то есть без лимитов это бесплатный рассыльщик чужой почты.
    Проверено: 14 запросов подряд → 14 писем. Цена ошибки не «спам», а блокировка
    домена в Unisender Go, после которой письма перестают доходить ОПЛАТИВШИМ.
    Поэтому: лимит по адресу И по IP, обрезка квиза по белому списку.

    Лимиты выбраны так, чтобы живой человек в них не упирался: квиз отправляет
    лид один раз за прохождение, 5 прохождений в час с одного адреса — это уже
    не человек. По IP лимит вчетверо шире: за одним адресом сидит и офисный
    wi-fi, и CGNAT мобильного оператора, а терять лид дороже, чем пропустить
    два десятка писем.
    """
    em = str(lead.email).strip().lower()
    # Свои прогоны (np_notrack=1) не лимитируем: они и так не пишут лид и не шлют
    # письмо — ограничивать нечего, а e2e гоняет воронку десятки раз подряд с
    # одного адреса и одного IP. Обойти защиту этим нельзя ровно поэтому.
    if not notrack() and not (
            _rate_ok("lead_email", em, 5, 3600) and _rate_ok("lead_ip", _client_ip(request), 20, 3600)):
        _bump("lead_rate_limited")
        # 429 квиз не ломает: он раскрывает план на любом ответе, кроме 422.
        return JSONResponse({"error": "too many"}, status_code=429)
    quiz = _clean_quiz(lead.quiz)
    marks = _marks(request)
    rec = lead.model_dump()
    rec["quiz"] = quiz
    rec["src"] = "ad" if _attr_src(request) == "ad" else (lead.src or "organic")
    if marks:
        rec["utm"] = marks
    rec["ts"] = datetime.now(timezone.utc).isoformat()
    slug = lead.landing if lead.landing in LANDINGS else "?"
    _bump(f"lead_{slug}")
    if notrack():
        # Свой прогон: ни строки в лидах, ни письма. Отвечаем как обычно —
        # фронт должен вести себя ровно так же, иначе тестируется не то.
        return JSONResponse({"ok": True})
    # Согласие фиксируем здесь, а не только на входе: именно на этом шаге человек
    # отдаёт нам пол, возраст, рост, вес, цель и пищевые ограничения (аллергии — это
    # данные о здоровье). Раньше запись была ровно одна — на /login, то есть по
    # квизу и по оплате согласия в журнале не было НИ ОДНОГО.
    _record_consent(request, em, "quiz_lead")
    _append_jsonl(LEADS, rec, "lead")
    # квиз-лид → письмо с готовой нормой + ссылкой СРАЗУ на пейволл (не на старт квиза заново)
    if quiz and lead.email:
        base = str(request.base_url).rstrip("/")
        ls = lead.landing if lead.landing in LANDINGS else DEFAULT_LANDING
        tok = _lead_token_make(str(lead.email), quiz)
        link = f"{base}/quiz?l={ls}&resume={tok}"  # resume → квиз восстановит норму и покажет пейволл
        if _lead_mail_budget_ok():
            bg.add_task(_send_lead, str(lead.email), quiz, link)
    return JSONResponse({"ok": True})


@app.get("/api/lead/resume/{token}")
def lead_resume(token: str, request: Request) -> JSONResponse:
    """Данные лида по resume-токену (для восстановления пейволла из письма).

    Токена в адресе больше нет — квиз присылает метку-заглушку, настоящий лежит
    в куке (см. quiz). Путь с токеном оставлен рабочим ради писем, отправленных
    до этой правки: их ссылки всё равно проходят через редирект, но обращение
    напрямую ломать незачем."""
    rec = _lead_token_get(token)
    if not rec:
        rec = _lead_token_get(request.cookies.get(LEAD_RESUME_COOKIE, ""))
    if not rec:
        return JSONResponse({"error": "expired"}, status_code=404)
    return JSONResponse({"email": rec["email"], "quiz": rec["quiz"]})


def _write_order(rec: dict) -> None:
    # Заказ — единственный след платежа на нашей стороне: по нему ищут оплату
    # вебхук, /pay/success и cron-досдача. Потерять его молча нельзя.
    _append_jsonl(ORDERS, rec, "order")


def _landing_slug(name: str) -> str:
    """Слаг лендинга из тела запроса — только из белого списка: он идёт в имена
    счётчиков, и произвольная строка из интернета там не нужна."""
    return name if name in LANDINGS else DEFAULT_LANDING


def _pay_tech_fail(kind: str, slug: str, why: str) -> None:
    """Платёж НЕ создан по нашей вине: нет ключей, ЮKassa ответила 5xx, исключение.

    До этого счётчика «касса лежит» и «человек передумал» выглядели одинаково — как
    отсутствие оплат: pay_init рос, pay_ok не рос, и разницы между сломанной кассой и
    плохой конверсией в данных не было вообще. Считаем и кричим в лог, чтобы неделю
    неработающей кассы нельзя было принять за неудачный тест лендинга."""
    _bump(f"{kind}_error")
    _bump(f"{kind}_error_{slug}")
    print(f"[ALERT] {kind}: платёж НЕ создан ({slug}): {why}", flush=True)


class PayReq(BaseModel):
    email: EmailStr
    landing: str = ""
    goal: str = ""
    quiz: dict = {}
    src: str = "organic"


def _order_src(req: PayReq, request: Request) -> str:
    """Источник заказа. Серверный вывод сильнее присланного телом: src в теле
    собирает страница квиза, а на квиз метки могли не доехать — ровно из-за
    этого КАЖДАЯ платная покупка ложилась в orders.jsonl как organic."""
    return "ad" if _attr_src(request) == "ad" else (req.src or "organic")


def _pay_rate_ok(req: PayReq, request: Request) -> bool:
    """Тот же класс, что и у /api/lead: ручка без авторизации создаёт платежи в
    ЮKassa и пишет строки на диск. Живой человек оформляет заказ один-два раза
    (мог передумать с тарифом, мог вернуться после «платёж не прошёл»), поэтому
    10 в час с адреса и 40 с IP он не увидит."""
    em = str(req.email).strip().lower()
    if _rate_ok("pay_email", em, 10, 3600) and _rate_ok("pay_ip", _client_ip(request), 40, 3600):
        return True
    _bump("pay_rate_limited")
    return False


@app.post("/api/pay/create")
def pay_create(req: PayReq, request: Request) -> JSONResponse:
    """Создать платёж в ЮKassa → вернуть URL страницы оплаты."""
    slug = _landing_slug(req.landing)
    if not (YOOKASSA_SHOP and YOOKASSA_SECRET):
        _pay_tech_fail("pay_create", slug, "нет ключей ЮKassa в окружении")
        return JSONResponse({"error": "payments not configured"}, status_code=503)
    if not _pay_rate_ok(req, request):
        return JSONResponse({"error": "too many"}, status_code=429)
    # Согласие — ДО обращения к ЮKassa: дальше почта и данные квиза уходят в чек
    # и в заказ. Оплативший был единственным, у кого записи о согласии не было
    # вообще, хотя обрабатываем мы у него больше всех.
    _record_consent(request, str(req.email), "pay")
    import uuid
    import requests
    src, marks = _order_src(req, request), _marks(request)
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
                      "email": req.email, "landing": slug, "goal": req.goal,
                      "quiz": _clean_quiz(req.quiz), "src": src, "utm": marks,
                      **_ad_ids(request, marks),
                      "amount": PRICE_RUB, "pay_url": url,
                      "ts": datetime.now(timezone.utc).isoformat()})
        _bump(f"pay_init_{slug}")
        _bump(f"pay_init_{slug}_{src}")
        if not url:
            _pay_tech_fail("pay_create", slug, f"ЮKassa не вернула confirmation_url (платёж {j.get('id')})")
            return JSONResponse({"error": "no url"}, status_code=502)
        return JSONResponse({"url": url})
    except Exception as e:  # noqa: BLE001
        _pay_tech_fail("pay_create", slug, f"{type(e).__name__}: {str(e)[:160]}")
        return JSONResponse({"error": "yookassa error"}, status_code=502)


def _regen_by_quiz(sid: str, quiz: dict, base: str) -> None:
    """Пересобрать план подписки под НОВЫЙ квиз (тот же токен: ссылка у человека уже есть,
    а второй план на одну подписку сделал бы старый недоступным из PWA)."""
    try:
        import plan
        import plan_ai
        sub = _load_sub(sid)
        token = sub.get("plan_token") or sid
        old = _load_plan(token)
        pl = plan_ai.generate_plan(quiz, avoid=list(dict.fromkeys(_menu_names(old))))
        pl["quiz"] = quiz
        # Отметки «приготовил» сбрасываем только вместе с меню — то же правило, что в
        # недельной регенерации: стереть неделю и оставить те же блюда нельзя.
        _save_plan(token, pl, reset_progress=_menu_names(pl) != _menu_names(old))
        link = f"{base}/plan/{token}"
        em = _plan_email(token)
        if pl.get("source") != "ai" or not em:
            _plan_mark(token, {"mail_owed": "requiz"})  # заготовку за готовый план не выдаём
        else:
            _pregen_dish_photos(pl)
            _send_email(em, _OWED_MAIL["requiz"][0],
                        plan.menu_email_html(pl, link), _OWED_MAIL["requiz"][1])
    except Exception as e:  # noqa: BLE001
        _bump("requiz_fail")
        print(f"[ALERT] пересборка плана по новому квизу не удалась: sub={sid} err={e}", flush=True)


@app.post("/api/pay/subscribe")
def pay_subscribe(req: PayReq, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Первый платёж подписки — с save_payment_method (сохранить способ для автосписаний)."""
    slug = _landing_slug(req.landing)
    if not (YOOKASSA_SHOP and YOOKASSA_SECRET):
        _pay_tech_fail("sub_create", slug, "нет ключей ЮKassa в окружении")
        return JSONResponse({"error": "payments not configured"}, status_code=503)
    if not _pay_rate_ok(req, request):
        return JSONResponse({"error": "too many"}, status_code=429)
    # См. /api/pay/create: согласие пишем до любой обработки, включая ветку
    # «подписка уже есть» — там мы принимаем НОВЫЙ квиз и пересобираем по нему план.
    _record_consent(request, str(req.email), "subscribe")
    import uuid
    import requests
    # Уже есть активная подписка на этот email → не создаём вторую (иначе вторая невидима
    # в ЛК и списывает деньги после «отмены» первой).
    #
    # Но и молча вернуть человека на СТАРЫЙ план нельзя: он прошёл квиз заново именно
    # потому, что изменились цель и вес, — и уходил на старое меню по старым ответам.
    # Выбор здесь такой: второй раз денег не берём (это оплата уже оплаченного), а
    # свежий квиз доезжает до плана — подписка ровно это и продаёт. Пересборка идёт в
    # фоне: ручка не должна ждать LLM, у человека на экране редирект.
    _em = str(req.email).strip().lower()
    # past_due наравне с active: это «списание не прошло, идут ретраи», подписка
    # живая и крон её обслуживает. Пока сюда попадал только active, человек в
    # grace-окне оформлял ВТОРУЮ подписку, а следующий тик крона списывал ещё раз
    # по старой — два списания и две подписки на один адрес.
    for s in _all_subs():
        if s.get("email", "").strip().lower() == _em \
                and s.get("status") in ("active", "past_due") \
                and s.get("payment_method_id"):
            base = str(request.base_url).rstrip("/")
            sid = s.get("sub_id") or ""
            # Подтверждаем, что это ХОЗЯИН адреса, а не кто-то, кто его знает.
            # Раньше ручка отдавала plan_url любому: по чужой почте выдавался
            # токен чужого плана, а токен — это отмена подписки и отвязка карты.
            # По той же причине под гейтом и пересборка: без него любой мог
            # гонять дорогие LLM-регенерации в чужом плане.
            acc = _current_account(request)
            owner = bool(acc) and _auth.norm_email(acc["email"]) == _em
            if not owner:
                _bump("sub_already_anon")
                return JSONResponse({"already": True, "requiz": False, "login": True})
            fresh = _clean_quiz(req.quiz)
            # Ограничение по частоте — против случайного двойного клика и против
            # дорогих LLM-пересборок: меню меняется от ответов, а не от числа заходов.
            requiz = bool(fresh) and fresh != (s.get("quiz") or {}) and _rate_ok("requiz", sid, 3, 86400)
            if requiz:
                _sub_merge(sid, {"quiz": fresh})  # следующая неделя тоже по новым ответам
                _bump("sub_requiz")
                bg.add_task(_regen_by_quiz, sid, fresh, base)
            return JSONResponse({"already": True, "requiz": requiz,
                                 "plan_url": f"{base}/plan/{s.get('plan_token','')}"})
    src, marks = _order_src(req, request), _marks(request)
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
                      "email": req.email, "landing": slug, "goal": req.goal,
                      "quiz": _clean_quiz(req.quiz), "src": src, "utm": marks,
                      **_ad_ids(request, marks),
                      "amount": SUB_PRICE_RUB, "pay_url": url,
                      "ts": datetime.now(timezone.utc).isoformat()})
        _bump(f"sub_init_{slug}")
        _bump(f"sub_init_{slug}_{src}")
        if not url:
            _pay_tech_fail("sub_create", slug, f"ЮKassa не вернула confirmation_url (платёж {j.get('id')})")
            return JSONResponse({"error": "no url"}, status_code=502)
        return JSONResponse({"url": url})
    except Exception as e:  # noqa: BLE001
        _pay_tech_fail("sub_create", slug, f"{type(e).__name__}: {str(e)[:160]}")
        return JSONResponse({"error": "yookassa error"}, status_code=502)


def _yk_get_payment(pid: str) -> dict:
    """Перепроверка платежа через API ЮKassa — НЕ доверяем телу вебхука (защита от подделки).

    При ТЕХНИЧЕСКОМ сбое (сеть, 5xx, нет ключей) возвращает {"_unknown": True}. Это не
    педантизм: «API ответил, что платёж не succeeded» и «мы не смогли спросить» — разные
    вещи. Схлопнув их в пустой словарь, вебхук отвечал 200 на моргнувшую сеть, ЮKassa
    считала уведомление доставленным и больше его не повторяла — оплата навсегда теряла
    план, письмо и подписку."""
    if not (YOOKASSA_SHOP and YOOKASSA_SECRET and pid):
        return {"_unknown": True}
    try:
        import requests
        r = requests.get(f"https://api.yookassa.ru/v3/payments/{pid}",
                         auth=(YOOKASSA_SHOP, YOOKASSA_SECRET), timeout=20)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return {}  # платежа нет — это ОТВЕТ (подделка), повторять уведомление незачем
    except Exception:  # noqa: BLE001
        pass
    return {"_unknown": True}


def _yk_unknown(v: dict) -> bool:
    """Мы НЕ ЗНАЕМ ответа ЮKassa (в отличие от «ответ отрицательный»). Пустой словарь
    тоже считаем незнанием: валидный платёж пустым не бывает."""
    return not v or bool(v.get("_unknown"))


def _webhook_retry(event: str, pid: str) -> JSONResponse:
    """Ответ 5xx на уведомление, которое мы не смогли перепроверить: ЮKassa повторит
    доставку. 200 здесь означал бы «обработано» — и событие терялось безвозвратно."""
    _bump("webhook_verify_fail")
    print(f"[ALERT] webhook {event}: перепроверка платежа {pid or '?'} НЕ УДАЛАСЬ "
          f"(API недоступен) — отвечаем 503, ждём повтора от ЮKassa", flush=True)
    return JSONResponse({"ok": False, "retry": True}, status_code=503)


@app.post("/api/pay/webhook")
async def pay_webhook(request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Обёртка над обработчиком: любое НЕОЖИДАННОЕ исключение — это оплата, которую мы
    не разнесли (нет плана, нет письма, нет подписки). Раньше оно уходило в 500 молча:
    в счётчиках это выглядело как «оплат нет», то есть неотличимо от «никто не покупает».
    Считаем, кричим в лог и отвечаем 5xx — ЮKassa повторит доставку."""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False}, status_code=400)
    try:
        # Тело разобрали здесь, а всю работу уводим в тредпул. Внутри обработчика
        # стоит БЛОКИРУЮЩИЙ поход в ЮKassa на 20 секунд, и в async-ручке он
        # останавливал весь событийный цикл: один мусорный POST без всякой
        # авторизации подвешивал сайт целиком (замер: главная отдавалась 19 с
        # вместо 0,002 с; три запроса подряд — 59 с). А поскольку мы просим
        # ЮKassa повторить неудачную доставку, блокировка воспроизводила сама
        # себя. Обработчик синхронный — starlette выполнит его в потоке.
        return await run_in_threadpool(_pay_webhook, payload, request, bg)
    except Exception as e:  # noqa: BLE001
        _bump("webhook_error")
        print(f"[ALERT] вебхук ЮKassa упал: {type(e).__name__}: {str(e)[:200]}", flush=True)
        return JSONResponse({"ok": False, "error": "internal"}, status_code=500)


def _pay_webhook(payload: dict, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Уведомления ЮKassa. URL зарегистрировать в ЛК на ТРИ события:
    payment.succeeded, payment.canceled, refund.succeeded.

    Тело уведомления НЕ доверенное — КАЖДОЕ событие перепроверяется через API по id.
    Для возврата это обязательно: без проверки подделанное уведомление гасило бы
    подписку живому плательщику."""
    obj = payload.get("object") or {}
    event = payload.get("event")

    # ---- возврат ------------------------------------------------------------
    # Возврат делается руками в ЛК ЮKassa, и раньше в наших данных он не менял
    # НИЧЕГО: подписка оставалась active с привязанной картой, и через 30 дней
    # cron списывал деньги у того, кому мы только что вернули. То есть наш
    # собственный возврат превращался в спор по платежу.
    if event == "refund.succeeded":
        pid = obj.get("payment_id", "")
        rid = obj.get("id", "")
        # Проверяем по API: возврат виден в самом платеже как refunded_amount.
        # Без этого поддельное уведомление гасило бы подписку живому плательщику.
        verified = _yk_get_payment(pid)
        if _yk_unknown(verified):
            # Спросить не удалось. Ответив 200, мы бы навсегда потеряли возврат: подписка
            # осталась бы active с картой, и через 30 дней cron списал бы с того, кому вернули.
            return _webhook_retry("refund.succeeded", pid)
        try:
            refunded = float((verified.get("refunded_amount") or {}).get("value") or 0)
        except Exception:  # noqa: BLE001
            refunded = 0.0
        if refunded <= 0:
            return JSONResponse({"ok": True, "verified": False, "reason": "no_refund"})
        if _already_processed(f"refund:{rid}"):
            return JSONResponse({"ok": True, "duplicate": True})
        order = _order_by_payment(pid)
        oid = order.get("order", "")
        now = datetime.now(timezone.utc)
        _write_order({"order": oid, "type": order.get("type", "?"), "payment_id": pid,
                      "refund_id": rid, "status": "refunded",
                      "amount": (obj.get("amount") or {}).get("value"),
                      "email": order.get("email", ""), "landing": order.get("landing", "?"),
                      "ts": now.isoformat(), "event": "refund.succeeded"})
        _bump(f"refund_{order.get('landing', '?')}")
        # Карту убираем и подписку гасим: списывать дальше с того, кому вернули, нельзя.
        if oid and _load_sub(oid):
            _sub_merge(oid, {"status": "canceled", "canceled_at": now.isoformat(),
                             "cancel_reason": "refund"}, remove=("payment_method_id",))
        print(f"[pay] возврат {rid} по платежу {pid} заказ={oid or '?'}", flush=True)
        return JSONResponse({"ok": True})

    # ---- платёж отклонён ----------------------------------------------------
    # Без этой ветки у платёжной воронки нет знаменателя: отклонённые банком
    # попытки были невидимы, и «мало оплат» нельзя было отличить от «оплаты не
    # проходят».
    if event == "payment.canceled":
        pid = obj.get("id", "")
        verified = _yk_get_payment(pid)
        if _yk_unknown(verified):
            return _webhook_retry("payment.canceled", pid)  # не знаем — пусть повторят
        if verified.get("status") != "canceled":
            return JSONResponse({"ok": True, "verified": False})
        obj = verified            # дальше только проверенные данные
        if _already_processed(f"canceled:{pid}"):
            return JSONResponse({"ok": True, "duplicate": True})
        meta = obj.get("metadata") or {}
        slug = meta.get("landing", "?")
        typ = meta.get("type", "once")
        reason = ((obj.get("cancellation_details") or {}).get("reason") or "?")
        _write_order({"order": meta.get("order", ""), "type": typ, "payment_id": pid,
                      "status": "canceled", "reason": reason,
                      "email": meta.get("email", ""), "landing": slug,
                      "ts": datetime.now(timezone.utc).isoformat(), "event": "payment.canceled"})
        _bump(f"{'sub_fail' if typ in ('subscription', 'sub_renew') else 'pay_fail'}_{slug}")
        # Регулярное списание не прошло: платёж мёртв, досматривать его больше нечего →
        # снимаем pending_charge_id. Статус переводим в past_due («идут ретраи», cron
        # добивает до next_charge+grace и потом завершает) и ТОЛЬКО из active: cron мог
        # уже увести подписку в ended, а человек — отменить её, и вебхук, ходивший тут
        # мимо _guarded_status, воскрешал чужое состояние поверх.
        oid = meta.get("order", "")
        if typ == "sub_renew" and oid and _load_sub(oid):
            _sub_merge(oid, {}, remove=("pending_charge_id",))
            _guarded_status(oid, "past_due")
        print(f"[pay] платёж {pid} отклонён ({reason}) тариф={typ}", flush=True)
        return JSONResponse({"ok": True})

    if event == "payment.succeeded":
        pid = obj.get("id", "")
        verified = _yk_get_payment(pid)
        if _yk_unknown(verified):
            # Раньше здесь молча уходило 200: сеть моргнула → ЮKassa считала уведомление
            # доставленным и не повторяла его, а плана, письма и подписки так и не было.
            return _webhook_retry("payment.succeeded", pid)
        if not (verified.get("status") == "succeeded" and verified.get("paid")):
            return JSONResponse({"ok": True, "verified": False})  # подделка/неоплачено — игнор
        obj = verified  # дальше используем ТОЛЬКО проверенные данные ЮKassa
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
        quiz = order.get("quiz") or {}
        # Источник берём ИЗ ЗАКАЗА: вебхук приходит от ЮKassa, ни куки, ни меток
        # клика в нём нет. Даёт разрез «сколько денег принесла реклама» прямо в
        # счётчиках, а не только при разборе orders.jsonl.
        osrc = order.get("src") or "organic"
        # Идемпотентность: повторная доставка того же payment_id → выходим сразу
        # (иначе сброс next_charge, второе письмо, дубль LLM, воскрешение отмены).
        # ИСКЛЮЧЕНИЕ: оплата есть, а файла плана нет — значит генерация не дожила до конца
        # (исключение внутри _fulfill_paid или рестарт контейнера). Раньше такой повтор
        # отбрасывался как дубль, и «оплачено, плана нет» становилось вечным; теперь повтор —
        # это шанс дозвать доставку. _fulfill_once не даст ни второго плана, ни второго письма.
        if _already_processed(pid):
            if typ in ("once", "subscription") and _plan_missing(oid):
                _bump("fulfill_retry_webhook")
                print(f"[ALERT] повтор вебхука по оплаченному заказу без плана — "
                      f"дозаказываем: order={oid}", flush=True)
                bg.add_task(_fulfill_once, email, quiz, oid, base)
                return JSONResponse({"ok": True, "duplicate": True, "refulfill": True})
            return JSONResponse({"ok": True, "duplicate": True})
        _write_order({"order": oid, "type": typ, "payment_id": obj.get("id"), "status": "succeeded",
                      "email": email, "landing": slug, "ts": now.isoformat(), "event": "payment.succeeded"})
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
            _bump(f"sub_ok_{slug}_{osrc}")
            # Письмо об активации — отдельно от письма с планом: человек должен из почты
            # знать сумму, дату следующего списания и куда идти отменять. В фон, чтобы
            # мейлер не съел таймаут вебхука (иначе ЮKassa начнёт ретраить доставку).
            s = _load_sub(oid)
            bg.add_task(_sub_mail_once, oid, "mail_started", "Подписка активирована · NutriPlan",
                        _sub_started_email(base, oid, s.get("amount", SUB_PRICE_RUB),
                                           s.get("next_charge", "")), "sub_started")
            bg.add_task(_fulfill_once, email, quiz, oid, base)  # даже при пустом quiz → bank-план + письмо
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
                bg.add_task(_mail_charged, s, obj.get("id", ""), base)  # с уже сдвинутой датой
        else:
            _bump(f"pay_ok_{slug}")
            _bump(f"pay_ok_{slug}_{osrc}")
            bg.add_task(_fulfill_once, email, quiz, oid, base)  # даже при пустом quiz
    return JSONResponse({"ok": True})


# ---------- аккаунт: вход по email (magic-link) + управление подпиской ----------

LOGINS = DATA / "logins.json"
LOGIN_LINK_TTL = 1800            # сколько ссылка ждёт первого открытия
LOGIN_LINK_AFTER_USE = 86400     # ...и сколько живёт после него (см. login_consume)


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
            f"<p style='color:#9aa39a;font-size:13px'>Ссылка ждёт 30 минут, а после первого открытия "
            f"работает ещё сутки. Если вход запрашивал не ты — просто проигнорируй это письмо.</p></div>")
    _send_email(email, "Вход в NutriPlan", html, "login")


class LoginReq(BaseModel):
    email: EmailStr
    # Что именно было показано над кнопкой и какой версии — присылает фронт,
    # чтобы в записи лежал реально увиденный текст, а не серверная догадка.
    consent_text: str = ""
    consent_version: str = ""


# ══════════════════════════════════════════════════════════════════════════
# Пароль и сессии
#
# Аккаунт заводится САМ в момент оплаты — человек для этого ничего не делает.
# Пароль предлагается сразу после оплаты, но НЕ является замком на оплаченном:
# закрыл вкладку на этом шаге — ссылка из письма всё равно откроет план, а
# экран пароля покажется в следующий раз. Иначе севший телефон между оплатой
# и паролем означал бы «заплатил и не получил», то есть возврат.
# ══════════════════════════════════════════════════════════════════════════


def _set_session_cookie(resp, sid: str) -> None:
    """SameSite=Lax, не Strict: возврат с ЮKassa на /pay/success — это кросс-сайтовая
    навигация, и при Strict кука не отправилась бы, показав разлогиненного человека
    сразу после оплаты."""
    resp.set_cookie(SESSION_COOKIE, sid, max_age=_auth.SESSION_DAYS * 86400,
                    httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")


def _current_account(request: Request):
    try:
        return AUTH.session_account(request.cookies.get(SESSION_COOKIE, ""))
    except Exception as e:  # noqa: BLE001
        print(f"[ALERT] сессия недоступна: {e}", flush=True)
        return None


def _same_origin(request: Request) -> bool:
    """Мутирующие ручки не должны исполняться по запросу с чужого сайта. Кука у нас
    Lax, то есть на кросс-сайтовый POST браузер её и так не пришлёт, но это второй
    слой — на случай, если кто-то однажды поставит SameSite=None."""
    site = request.headers.get("sec-fetch-site", "")
    if site:
        return site in ("same-origin", "same-site", "none")
    origin = request.headers.get("origin", "")
    if not origin:
        return True                       # не браузер (curl, вебхук) — не наш случай
    return origin.rstrip("/") == str(request.base_url).rstrip("/")


class PwReq(BaseModel):
    password: str
    password2: str = ""


@app.post("/api/auth/password")
def auth_set_password(req: PwReq, request: Request) -> JSONResponse:
    """Задать пароль сразу после оплаты.

    ГРАНИЦА ДОВЕРИЯ, и она узкая намеренно. Кука заказа — слабое доказательство:
    её получает любой, кто открыл /pay/success?o=<токен>, а токен лежит в каждом
    письме, в истории браузера и в ссылках, которыми люди делятся сами. Поэтому
    по куке можно ТОЛЬКО завести первый пароль на аккаунте, у которого его ещё
    нет, и только по свежей оплате. СМЕНА существующего пароля — исключительно
    по сессии или по коду из письма (/api/auth/reset).

    Без этого получался захват аккаунта: посторонний со ссылкой на план задавал
    свой пароль на чужую почту, входил и выбивал владельца (его сессии при смене
    пароля закрываются). Проверено воспроизведением.
    """
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "Обнови страницу и попробуй ещё раз"}, status_code=403)
    acc = _current_account(request)
    email = acc["email"] if acc else ""
    by_order = False
    if not email:
        oid = "".join(c for c in request.cookies.get(PAY_ORDER_COOKIE, "") if c.isalnum())[:40]
        order = _find_order(oid) if oid else {}
        # Заказ должен быть настоящим и оплаченным: сама кука ничего не доказывает.
        if not order or not (PLANS / f"{oid}.json").exists():
            pid = order.get("payment_id", "")
            if not pid or _yk_get_payment(pid).get("status") != "succeeded":
                return JSONResponse({"ok": False, "error": "Не видим оплаченного заказа"}, status_code=403)
        # Окно в сутки: форма живёт на экране сразу после оплаты, а не «когда-нибудь
        # потом по старой ссылке». Чем уже окно, тем меньше шанс, что чужой успеет
        # раньше хозяина.
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(str(order.get("ts") or ""))).total_seconds()
        except Exception:  # noqa: BLE001
            age = 10 ** 9
        if age > 86400:
            return JSONResponse({"ok": False, "code_hint": True,
                                 "error": "Ссылка на оплату уже не свежая. Задай пароль через "
                                          "«Забыли пароль» — пришлём код на почту"}, status_code=403)
        email = order.get("email", "")
        by_order = True
    if by_order and AUTH.has_password(email):
        # Пароль уже есть — значит хозяин им пользуется. Перебить его по куке
        # заказа нельзя: именно так и выглядел захват.
        _bump("pw_set_denied_existing")
        return JSONResponse({"ok": False, "code_hint": True,
                             "error": "На этой почте уже есть пароль. Войди с ним или "
                                      "нажми «Забыли пароль» — пришлём код"}, status_code=409)
    if not _auth.valid_email(email):
        return JSONResponse({"ok": False, "error": "Не видим оплаченного заказа"}, status_code=403)
    # Пишем ПОСЛЕ проверки права на заказ, а не до неё. Адрес здесь взят с сервера
    # (из заказа или сессии), и запись означает ровно то, что написано, — в отличие
    # от журнала, куда посторонний мог вписать чужую почту одним POST.
    _record_consent(request, email, "password_set")
    pw, pw2 = req.password or "", req.password2 or ""
    if pw2 and pw != pw2:
        return JSONResponse({"ok": False, "error": "Пароли не совпадают"}, status_code=422)
    why = _auth.Auth.password_problem(pw, email)
    if why:
        return JSONResponse({"ok": False, "error": why}, status_code=422)
    aid = AUTH.ensure_account(email)
    AUTH.set_password(email, pw)
    if not by_order:
        # Смена пароля из своей сессии выкидывает остальные входы — это её смысл.
        # А вот при ПЕРВОЙ установке по куке заказа рубить чужие сессии нельзя:
        # именно этим захватчик и выбивал владельца.
        AUTH.close_all_sessions(aid)
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, AUTH.open_session(aid))
    _bump("pw_set")
    return resp


class AuthLoginReq(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login")
def auth_login(req: AuthLoginReq, request: Request) -> JSONResponse:
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "Обнови страницу и попробуй ещё раз"}, status_code=403)
    email = _auth.norm_email(req.email)
    ip = _client_ip(request)
    # Блокируем ФОРМУ ПАРОЛЯ, а не аккаунт: вход кодом из письма продолжает
    # работать. Иначе любой желающий выключал бы чужой вход пятью попытками.
    if not AUTH.rate_ok("pw_try_email", email, _auth.PW_TRIES, _auth.PW_TRIES_WINDOW):
        _bump("pw_try_blocked")
        return JSONResponse({"ok": False, "code_hint": True,
                             "error": "Слишком много попыток. Войди по коду из письма "
                                      "или попробуй через 15 минут"}, status_code=429)
    if not AUTH.rate_ok("pw_try_ip", ip, 30, 3600):
        return JSONResponse({"ok": False, "error": "Слишком много попыток"}, status_code=429)
    if not AUTH.check_password(email, req.password or ""):
        # Один и тот же текст для «нет аккаунта» и «неверный пароль»: иначе форма
        # входа превращается в способ узнать, кто у нас покупал.
        return JSONResponse({"ok": False, "error": "Неверная почта или пароль"}, status_code=401)
    # Только на УДАВШЕМСЯ входе: на неудачной попытке адрес прислал кто угодно, и
    # строка «этот человек согласился» была бы записью о переборщике, а не о владельце.
    _record_consent(request, email, "login_password")
    acc = AUTH.account(email)
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, AUTH.open_session(int(acc["id"])))
    _bump("pw_login")
    return resp


class EmailOnlyReq(BaseModel):
    email: str


@app.post("/api/auth/forgot")
def auth_forgot(req: EmailOnlyReq, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Код для сброса пароля.

    Защита от рассылки писем чужим адресам построена так, что вектор исчезает,
    а не ограничивается: письмо уходит ТОЛЬКО на адрес, у которого уже есть
    аккаунт. Ответ при этом всегда одинаковый — иначе форма становится способом
    перебором выяснить, кто у нас покупал.
    """
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "Обнови страницу"}, status_code=403)
    email = _auth.norm_email(req.email)
    ip = _client_ip(request)
    neutral = JSONResponse({"ok": True})
    if not _auth.valid_email(email):
        return neutral
    # Лимиты живут в базе, а не в памяти процесса: память обнуляется деплоем,
    # то есть лимит обходился бы ожиданием выкладки.
    if not (AUTH.rate_ok("forgot_email_h", email, _auth.FORGOT_PER_EMAIL_HOUR, 3600)
            and AUTH.rate_ok("forgot_email_d", email, _auth.FORGOT_PER_EMAIL_DAY, 86400)
            and AUTH.rate_ok("forgot_ip", ip, _auth.FORGOT_PER_IP_HOUR, 3600)):
        _bump("forgot_rate_limited")
        # Говорим правду: письма НЕ БУДЕТ. Раньше экран рисовал «код уже летит»,
        # и человек, у которого письмо ушло в спам, тремя нажатиями доводил себя
        # до состояния «страница обещает, письма нет, ждать час».
        return JSONResponse({"ok": True, "capped": True})
    # Общий суточный потолок — защита не от одного злоумышленника, а от репутации
    # домена: заблокируют отправку, и письма перестанут доходить ОПЛАТИВШИМ.
    if not AUTH.rate_ok("forgot_global", "all", _auth.FORGOT_GLOBAL_DAY, 86400):
        _bump("forgot_global_capped")
        print("[ALERT] суточный потолок писем восстановления исчерпан", flush=True)
        return neutral
    if not AUTH.account(email):
        # Аккаунтов в SQLite нет у всех, кто купил ДО появления входа. Для них
        # «Забыли пароль» молча не делал ничего: вся ранее оплатившая база
        # осталась без доступа к своему плану. Заводим аккаунт на лету, если
        # человек нашёлся среди заказов и подписок — это те же данные, по которым
        # работал прежний вход по ссылке.
        if _find_account(email).get("plan_token"):
            AUTH.ensure_account(email)
            _bump("account_backfilled")
        else:
            _bump("forgot_no_account")
            # Письма нет — и вектора рассылки нет. Но ответ обязан выглядеть так
            # же, как у существующего аккаунта, включая паузу перед повтором:
            # иначе разница в теле ответа отвечает на вопрос «а этот у вас
            # покупал?». Паузу считаем по тому же счётчику, что и лимиты.
            n = AUTH.hit_count("forgot_email_h", email, _auth.CODE_RESEND_SEC)
            return JSONResponse({"ok": True, "wait": _auth.CODE_RESEND_SEC - 1} if n > 1
                                else {"ok": True})
    # Аккаунт существует и мы шлём на него письмо — то есть обрабатываем ПДн
    # конкретного человека. По чужому адресу сюда не дойти: ветка выше отсекла.
    _record_consent(request, email, "password_recovery")
    code, wait = AUTH.issue_code(email, "reset")
    if not code:
        # Живой код уже выдан. Второго письма НЕ шлём: иначе кнопка «отправить
        # ещё раз» становится усилителем — один нажимающий, сколько угодно писем.
        # wait возвращаем ВСЕГДА, а не только существующим аккаунтам: иначе само
        # его наличие отвечает на вопрос «есть ли у вас такой клиент», и форма
        # снова становится способом перечислить покупателей (см. ниже —
        # несуществующему адресу отдаётся такой же ответ).
        return JSONResponse({"ok": True, "wait": wait})
    bg.add_task(_send_code_email, email, code)
    _bump("forgot_sent")
    return neutral


class ResetReq(BaseModel):
    email: str
    code: str
    password: str
    password2: str = ""


@app.post("/api/auth/reset")
def auth_reset(req: ResetReq, request: Request) -> JSONResponse:
    if not _same_origin(request):
        return JSONResponse({"ok": False, "error": "Обнови страницу"}, status_code=403)
    email = _auth.norm_email(req.email)
    if not AUTH.rate_ok("reset_ip", _client_ip(request), 30, 3600):
        return JSONResponse({"ok": False, "error": "Слишком много попыток"}, status_code=429)
    why = AUTH.check_code(email, req.code or "", "reset")
    if why:
        return JSONResponse({"ok": False, "error": why}, status_code=422)
    pw, pw2 = req.password or "", req.password2 or ""
    if pw2 and pw != pw2:
        return JSONResponse({"ok": False, "error": "Пароли не совпадают"}, status_code=422)
    bad = _auth.Auth.password_problem(pw, email)
    if bad:
        # Код уже погашен проверкой выше — вернуть его нельзя, поэтому честно
        # говорим, что нужен новый. Иначе человек будет вводить сгоревший код.
        return JSONResponse({"ok": False, "error": bad + ". Запроси новый код и попробуй ещё раз"},
                            status_code=422)
    # Код из письма уже проверен — владение адресом доказано, запись не подделать.
    _record_consent(request, email, "password_reset")
    aid = AUTH.ensure_account(email)
    AUTH.set_password(email, pw)
    AUTH.close_all_sessions(aid)   # сброс пароля выкидывает того, кто мог войти раньше
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, AUTH.open_session(aid))
    _bump("pw_reset")
    return resp


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> JSONResponse:
    sid = request.cookies.get(SESSION_COOKIE, "")
    if sid:
        AUTH.close_session(sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


def _send_code_email(email: str, code: str) -> None:
    """Письмо с кодом. Формулировка буквальная: нетехническая аудитория путает
    код входа с кодом банка, а этим пользуются телефонные мошенники."""
    pretty = f"{code[:3]} {code[3:]}"
    html = (
        "<div style='font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px;"
        "margin:0 auto;padding:32px;color:#20321F'>"
        "<h2 style='margin:0 0 12px'>Код для входа в NutriPlan</h2>"
        "<p style='color:#6B7566;margin:0 0 18px'>Введи его на странице входа, чтобы задать новый пароль:</p>"
        f"<div style='font-size:34px;font-weight:800;letter-spacing:.12em;background:#F1F7EE;"
        f"border-radius:16px;padding:18px;text-align:center'>{pretty}</div>"
        "<p style='color:#6B7566;font-size:14px;margin:18px 0 0'>Код действует 10 минут.</p>"
        "<p style='color:#6B7566;font-size:14px;margin:10px 0 0'><b>Мы никогда не спросим этот код "
        "по телефону или в переписке.</b> Если ты не запрашивал вход — просто удали письмо, "
        "пароль останется прежним.</p></div>")
    _send_email(email, f"Код для входа: {pretty}", html, "login_code")


@app.post("/api/login/request")
def login_request(req: LoginReq, request: Request, bg: BackgroundTasks) -> JSONResponse:
    # Rate-limit: не даём перебирать аккаунты и бомбить почту письмами.
    #
    # Было 3 письма в час и МОЛЧАЛИВЫЙ ok:true сверх лимита — страница рисовала
    # успех, письма не было, и человек ждал его до вечера. Порог поднят (письмо
    # проваливается в «Промоакции», и повторный запрос — нормальное поведение,
    # а не атака), а сверх лимита отвечаем честным 429 с текстом. Наличие
    # аккаунта это по-прежнему не раскрывает: лимит про запрашивающего.
    em = str(req.email).strip().lower()
    if not _rate_ok("login_email", em, 6, 3600):
        _bump("login_rate_limited")
        return JSONResponse({"ok": False, "error": "Мы уже отправили несколько писем на этот адрес. "
                                                   "Проверь входящие и «Промоакции» — следующее "
                                                   "письмо можно запросить через час."}, status_code=429)
    if not _rate_ok("login_ip", _client_ip(request), 20, 3600):
        _bump("login_rate_limited")
        return JSONResponse({"ok": False, "error": "Слишком много запросов входа. "
                                                   "Попробуй ещё раз через час."}, status_code=429)
    # Пишем ДО ветвления по наличию аккаунта: согласие человек дал нажатием,
    # независимо от того, нашёлся ли у него план. Ветка «аккаунта нет» тоже
    # обрабатывает его почту — значит и основание на неё нужно.
    _record_consent(request, em, "login_link", text=req.consent_text, version=req.consent_version)
    acct = _find_account(str(req.email))
    if acct.get("plan_token"):
        import uuid
        now = datetime.now(timezone.utc).timestamp()
        d = {k: v for k, v in _logins_load().items() if v.get("exp", 0) > now}
        token = uuid.uuid4().hex
        d[token] = {"email": str(req.email), "exp": now + LOGIN_LINK_TTL}
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
    # Ссылку НЕ гасим первым же GET. По ней ходит не только человек: антивирус
    # почтовика и предпросмотр мессенджера открывают ссылки из письма РАНЬШЕ
    # адресата — и одноразовая ссылка сгорала до того, как её кто-то увидел
    # («Ссылка устарела» на первом же клике). Это тот же класс дефекта, что был
    # у /sub/cancel: GET обязан быть безопасным.
    #
    # Вместо гашения: открываем сессию (дальше вход по куке, ссылка не нужна) и
    # продлеваем саму ссылку на сутки от первого использования — чтобы человек,
    # открывший письмо через час после прогрева, всё-таки попал внутрь.
    if not rec.get("used"):
        rec["used"] = datetime.now(timezone.utc).isoformat()
        rec["exp"] = now + LOGIN_LINK_AFTER_USE
        d[token] = rec
        _logins_save(d)
    acct = _find_account(rec["email"])
    if acct.get("plan_token"):
        resp = RedirectResponse(f"/plan/{acct['plan_token']}", status_code=302)
        try:
            _set_session_cookie(resp, AUTH.open_session(AUTH.ensure_account(rec["email"])))
        except Exception as e:  # noqa: BLE001
            # Сессия — удобство, а план по ссылке всё равно откроется: не роняем вход.
            print(f"[ALERT] вход по ссылке: сессия не открыта ({e})", flush=True)
        return resp
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
        # .lnk перебивает общее правило button{} выше: «Забыли пароль» — это
        # ссылка-действие, а не вторая зелёная кнопка рядом с «Войти».
        ".lnk{background:none;border:0;color:#0E7A36;font-size:14px;text-decoration:underline;"
        "text-underline-offset:3px;cursor:pointer;margin-top:14px;padding:0;width:auto;font-weight:600}"
        ".sub{color:#8A9384;font-size:13px;margin-top:10px}"
        "[hidden]{display:none!important}"
        "</style></head>"
        "<body><div class='c'><div class='b'><span class='dot'></span>NutriPlan</div>"

        # ── шаг 1: почта и пароль ───────────────────────────────────────────
        "<div id='st-pw'>"
        "<h1>Вход в приложение</h1><p>Почта и пароль от твоего плана.</p>"
        "<input type='email' id='m' placeholder='твой@email.ru' autocomplete='email' inputmode='email'>"
        "<input type='password' id='p' placeholder='Пароль' autocomplete='current-password' "
        "style='margin-top:8px'>"
        "<div class='err' id='e'></div>"
        # Согласие — НЕПОСРЕДСТВЕННО над кнопкой, и так в каждом шаге. Экранов
        # стало два, и подпись, оставленная внизу страницы, оказалась под
        # кнопкой: формально она на странице есть, а как «согласие действием»
        # уже не работает.
        f"<div class='cns'>{CONSENT_HTML}</div>"
        "<button id='s'>Войти</button>"
        "<button class='lnk' id='forgot' type='button'>Забыли пароль?</button>"
        "<div class='sub'>Ещё не задавал пароль? Нажми «Забыли пароль» — пришлём код.</div>"
        "</div>"

        # ── шаг 2: код из письма ────────────────────────────────────────────
        "<div id='st-code' hidden>"
        "<h1>Код из письма</h1>"
        "<p id='codehint'>Если на эту почту есть план — код уже летит. "
        "Проверь входящие и «Промоакции».</p>"
        "<input id='code' inputmode='numeric' autocomplete='one-time-code' maxlength='6' "
        "placeholder='6 цифр' style='text-align:center;letter-spacing:.3em;font-size:22px'>"
        "<input type='password' id='np1' placeholder='Новый пароль' autocomplete='new-password' "
        "style='margin-top:8px'>"
        "<input type='password' id='np2' placeholder='Повтори пароль' autocomplete='new-password' "
        "style='margin-top:8px'>"
        "<div class='err' id='e2'></div>"
        f"<div class='cns'>{CONSENT_HTML}</div>"
        "<button id='s2'>Сохранить и войти</button>"
        "<button class='lnk' id='again' type='button'>Отправить код ещё раз</button>"
        "<button class='lnk' id='back' type='button'>Назад ко входу</button>"
        "</div>"

        "<footer><a href='/privacy'>Политика</a><a href='/consent'>Согласие</a><a href='/offer'>Оферта</a></footer>"

        "<script>const $=s=>document.querySelector(s);"
        "const okmail=v=>/^[^\\s@]+@[^\\s@]+\\.[^\\s@]{2,}$/.test(v);"
        "function err(el,t){el.textContent=t;el.classList.add('s');}"
        "function clr(el){el.classList.remove('s');}"
        "function post(u,b){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify(b)}).then(r=>r.json().then(j=>({s:r.status,j:j})));}"

        # вход по паролю
        "$('#s').onclick=async()=>{const v=$('#m').value.trim(),p=$('#p').value;"
        "clr($('#e'));if(!okmail(v)){err($('#e'),'Проверь адрес почты');return;}"
        "if(!p){err($('#e'),'Введи пароль');return;}"
        "$('#s').disabled=true;$('#s').textContent='Вхожу…';"
        "try{const r=await post('/api/auth/login',{email:v,password:p});"
        "if(r.j.ok){location.href='/app';return;}"
        # Когда форму пароля заблокировали за перебор, честно уводим на код —
        # иначе человек упирается в стену, у которой нет двери.
        "err($('#e'),r.j.error||'Не удалось войти');"
        "if(r.j.code_hint){$('#forgot').click();}}catch(e){err($('#e'),'Нет связи');}"
        "$('#s').disabled=false;$('#s').textContent='Войти';};"

        # запрос кода
        "async function ask(){const v=$('#m').value.trim();"
        "if(!okmail(v)){err($('#e'),'Сначала введи адрес почты');return false;}"
        "clr($('#e'));const r=await post('/api/auth/forgot',{email:v});"
        # wait приходит, когда живой код уже выдан: второго письма не шлём, и
        # честно говорим об этом, а не рисуем успех поверх неотправленного.
        "if(r.j&&r.j.capped){$('#codehint').textContent='Мы уже отправляли код на этот адрес '"
        "+'несколько раз. Проверь входящие и «Промоакции» — новое письмо можно запросить '"
        "+'через час. Если письма нет совсем, напиши на support@mynutriplan.ru.';}"
        "else if(r.j&&r.j.wait){$('#codehint').textContent='Письмо уже отправляли — проверь входящие '"
        "+'и «Промоакции». Отправить ещё раз можно через '+r.j.wait+' сек.';}"
        "else{$('#codehint').textContent='Если на эту почту есть план — код уже летит. '"
        "+'Проверь входящие и «Промоакции».';}"
        "return true;}"
        "$('#forgot').onclick=async()=>{if(await ask()){"
        "$('#st-pw').hidden=true;$('#st-code').hidden=false;$('#code').focus();}};"
        "$('#again').onclick=()=>ask();"
        "$('#back').onclick=()=>{$('#st-code').hidden=true;$('#st-pw').hidden=false;};"

        # код + новый пароль
        "$('#s2').onclick=async()=>{clr($('#e2'));"
        "const b={email:$('#m').value.trim(),code:$('#code').value.trim(),"
        "password:$('#np1').value,password2:$('#np2').value};"
        "if(b.code.length<6){err($('#e2'),'Введи 6 цифр из письма');return;}"
        "$('#s2').disabled=true;$('#s2').textContent='Сохраняю…';"
        "try{const r=await post('/api/auth/reset',b);"
        "if(r.j.ok){location.href='/app';return;}err($('#e2'),r.j.error||'Не получилось');}"
        "catch(e){err($('#e2'),'Нет связи');}"
        "$('#s2').disabled=false;$('#s2').textContent='Сохранить и войти';};"
        "</script></div></body></html>"))


@app.get("/app")
def app_home(request: Request):
    """Приложение по адресу без токена. Пока это редирект на план, найденный по
    сессии: полный переезд URL — отдельная задача, а войти по паролю человек
    должен уметь уже сейчас."""
    acc = _current_account(request)
    if not acc:
        return RedirectResponse("/login", status_code=302)
    found = _find_account(acc["email"])
    token = found.get("plan_token") or ""
    if not token:
        # Аккаунт есть, плана нет: так бывает у того, кто задал пароль, но чей
        # план не собрался. Отправляем туда, где ему помогут, а не в 404.
        return RedirectResponse("/pay/success", status_code=302)
    return RedirectResponse(f"/plan/{token}", status_code=302)


@app.post("/api/sub/{token}/cancel")
def sub_cancel_api(token: str, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Отмена подписки + отвязка карты (требование ЮKassa). Отменяет ВСЕ активные подписки
    этого email — иначе «скрытая» вторая подписка продолжала бы списывать после отмены."""
    sub = _load_sub(token)
    if not sub:
        return JSONResponse({"error": "no sub"}, status_code=404)
    email = (sub.get("email") or "").strip().lower()
    n = 0
    for s in list(_all_subs()):
        same = (s.get("email", "").strip().lower() == email) if email else (s.get("sub_id") == token)
        # past_due — тоже живая подписка: в grace-окне cron продолжает пытаться списать.
        # Пока сюда попадал только active, отмена из этого состояния зависела от
        # запасной ветки ниже и не гасила вторую подписку того же человека.
        if same and s.get("status") in ("active", "past_due"):
            _sub_merge(s["sub_id"], {"status": "canceled", "payment_method_id": ""})
            n += 1
    if n == 0:  # на всякий — точечно
        _sub_merge(token, {"status": "canceled", "payment_method_id": ""})
    _bump(f"sub_cancel_{sub.get('landing','?')}")
    # Письмо-подтверждение: нажатие «Отменить» не оставляло НИКАКОГО следа на почте,
    # и через месяц человек шёл в банк блокировать карту, потому что не помнил, отменил
    # ли. Однократность — общий _sub_mail_once (метка в файле подписки).
    # Демо-подписку (её проверяющие ЮKassa жмут сколько угодно) обходим стороной:
    # она восстанавливается при каждом открытии страницы вместе с меткой письма,
    # то есть однократность на ней не работает и письма пошли бы пачкой.
    if token not in DEMO_TOKENS:
        bg.add_task(_sub_mail_once, token, "mail_canceled", "Подписка отменена · NutriPlan",
                    _sub_canceled_email(str(request.base_url).rstrip("/"), token,
                                        sub.get("next_charge", "")), "sub_canceled")
    return JSONResponse({"ok": True, "until": sub.get("next_charge", ""), "canceled": n or 1,
                         # Свежее состояние прямо в ответе: иначе экран после отмены
                         # остаётся со старой строкой про списание, пока не перезагрузят.
                         "view": _sub_view(_load_sub(token))})


@app.post("/api/sub/{token}/unbind-card")
def sub_unbind_card_api(token: str, request: Request, bg: BackgroundTasks) -> JSONResponse:
    """Отвязка карты БЕЗ отмены подписки (требование ЮKassa): автосписаний не будет,
    доступ сохраняется до конца оплаченного периода."""
    sub = _load_sub(token)
    if not sub:
        return JSONResponse({"error": "no sub"}, status_code=404)
    sub["payment_method_id"] = ""  # только отвязка способа; status остаётся active до конца периода
    _save_sub(sub)
    _bump(f"card_unbind_{sub.get('landing','?')}")
    # Отвязка — не отмена, и письмо об этом должно прийти отдельным текстом:
    # подписка ещё работает, просто автосписаний не будет.
    if token not in DEMO_TOKENS:   # см. отмену выше: демо сбрасывается и метка не держится
        bg.add_task(_sub_mail_once, token, "mail_unbound", "Карта отвязана · NutriPlan",
                    _sub_unbound_email(str(request.base_url).rstrip("/"), token,
                                       sub.get("next_charge", "")), "sub_unbound")
    return JSONResponse({"ok": True, "until": sub.get("next_charge", ""),
                         "view": _sub_view(_load_sub(token))})


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
    # Тексты и доступные кнопки считает сервер (_sub_view), а не вёрстка: она знала
    # только «active / всё остальное» и любому неактивному писала «Отменена ·
    # списаний больше не будет» — при past_due это ложь, там как раз идут ретраи.
    # Старые ключи (status/next/amount/has_card) сохранены, чтобы правка сервера и
    # правка страницы могли выкатываться независимо.
    subinfo = _sub_view(sub) if sub else None
    # Счётчика здесь НЕТ намеренно. Токен в адресе — единственный пароль к плану,
    # и по нему же работают отмена подписки и отвязка карты; Метрика шлёт page-url
    # целиком, а отчёт «Популярное» открывается гостевым доступом — счётчик выдавал
    # готовый список рабочих токенов живых клиентов (проверено перехватом:
    # watch/<id>?page-url=…/plan/a1b2…secrettoken99).
    #
    # Обезличенного хита мало: при defer:true Метрика всё равно отправляет
    # технический запрос с настоящим адресом (nohit=1) — замерено там же. Целей на
    # этой странице нет (npGoal никто не зовёт), терять нечего. Вернуть аналитику
    # можно будет после обмена токена на HttpOnly-куку — тогда секрета в адресе
    # не станет.
    return HTMLResponse(_no_referrer_leak(plan.page_html(pl, token=safe, sub=subinfo)))


class SwapReq(BaseModel):
    day: int
    slot: str
    idx: int | None = None      # номер приёма в дне; слоты повторяются («Перекус» ×2)


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
    # Сначала по номеру приёма, и только потом по названию слота: «Перекус» в дне
    # бывает дважды, и поиск по слоту заменял ПЕРВЫЙ — человек жал «Заменить» на
    # втором перекусе, а менялся первый.
    if req.idx is not None and 0 <= req.idx < len(meals) and meals[req.idx].get("slot") == req.slot:
        idx = req.idx
    else:
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


class SwapDayReq(BaseModel):
    day: int


@app.post("/api/plan/{token}/swap-day")
def plan_swap_day(token: str, req: SwapDayReq, bg: BackgroundTasks) -> JSONResponse:
    """Заменить ВЕСЬ день целиком: состав приёмов и калорийность те же, блюда новые.

    Лимит жёстче, чем у замены блюда: один запрос — это целый день генерации, и
    перебирать дни «пока не понравится» стоило бы дороже самой подписки.
    """
    if not _rate_ok("swapday", "".join(c for c in token if c.isalnum())[:40], 8, 3600):
        return JSONResponse({"error": "too many"}, status_code=429)
    pl = _load_plan(token)
    days = pl.get("days") or []
    if not (0 <= req.day < len(days)):
        return JSONResponse({"error": "bad day"}, status_code=400)
    meals = days[req.day].get("meals") or []
    if not meals:
        return JSONResponse({"error": "no meals"}, status_code=404)
    import plan_ai
    new = plan_ai.swap_day(pl.get("quiz") or {}, meals)
    if not new:
        return JSONResponse({"error": "gen failed"}, status_code=502)
    days[req.day]["meals"] = new
    _save_plan("".join(c for c in token if c.isalnum()), pl)
    for m in new:      # фото к моменту, когда человек долистает до дня
        bg.add_task(dish_photos.generate, dish_photos.slugify(m.get("name", "")), m.get("name", ""))
    return JSONResponse({"meals": new})


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
    # Настройки должны пережить недельную регенерацию: cron собирает следующую неделю по
    # quiz из файла ПОДПИСКИ, а не из файла плана. Без этой записи через 7 дней возвращались
    # старый вес, старая цель и пустой exclude — то есть исключённые продукты (для многих это
    # жёсткий стоп-лист: аллергия) снова оказывались в меню.
    _sid = _sub_by_plan_token(safe)
    if _sid:
        _sub_merge(_sid, {"quiz": quiz})
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
    """Готов ли план (для поллинга success-страницы после оплаты).

    Отдаём ещё и версию: после повторной оплаты с новыми ответами план на экране
    уже есть — он просто СТАРЫЙ, и «готов/не готов» тут ничего не различает.
    Страница плана ждёт именно смены версии.
    """
    tok = "".join(c for c in token if c.isalnum())
    f = PLANS / f"{tok}.json" if tok else None
    ready = bool(f and f.exists())
    ver = ""
    if ready:
        try:
            ver = str((json.loads(f.read_text(encoding="utf-8")) or {}).get("ver", "") or "")
        except Exception:  # noqa: BLE001
            ver = ""        # битый файл — пусть страница просто продолжит ждать
    return JSONResponse({"ready": ready, "ver": ver})


# Вычистить ?o= из адреса ДО того, как загрузится счётчик: в нём тот же токен,
# что и в адресе плана (файл плана лежит как {order}.json), а Метрика шлёт
# page-url целиком. Скрипт синхронный и стоит выше сниппета Метрики, tag.js
# грузится асинхронно — то есть к моменту чтения location адрес уже чистый.
# Одна константа на все три экрана возврата: два из них (ниже) собирались без
# неё, и токен заказа уезжал в Метрику именно с них.
_PAY_URL_SCRUB = "<script>try{history.replaceState(null,'','/pay/success');}catch(e){}</script>"


def _pay_unknown_page() -> HTMLResponse:
    """Заказ или платёж не нашлись — API молчит, ссылка старая, заказа нет.
    Раньше в этом случае показывалось «Оплата получена!»: мы утверждали факт
    списания, ничего о нём не зная. Лучше честно признать неопределённость и
    дать канал связи, чем угадать в пользу приятного варианта."""
    return HTMLResponse(_inject_metrika(_no_referrer_leak(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        + _PAY_URL_SCRUB +
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
        # Адрес хита свой, а не /pay/success: с общим адресом цель «URL содержит
        # /pay/success» засчитывала бы ненайденный платёж как оплату.
        "</div></body></html>"), anon_page="/pay/unknown"), status_code=200)


def _pay_failed_page(oid: str = "") -> HTMLResponse:
    """Отказ банка. oid нужен только чтобы положить куку заказа: адрес страницы
    вычищается от ?o=, и без куки F5 показал бы «Не нашли платёж» вместо отказа."""
    resp = HTMLResponse(_inject_metrika(_no_referrer_leak(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        + _PAY_URL_SCRUB +
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
        # Свой адрес хита, а не /pay/success: иначе цель «URL содержит /pay/success»
        # считала бы отказ банка оплатой.
        "</body></html>"), anon_page="/pay/failed"),
        status_code=200)
    if oid:
        # path='/', а не '/pay/success': по этой куке форма «Придумай пароль»
        # доказывает право на аккаунт, а живёт форма на той же странице, но
        # стучится в /api/auth/password. С узким path кука туда не долетала, и
        # задать пароль после оплаты не мог НИКТО — все шли через «Забыли пароль».
        resp.set_cookie(PAY_ORDER_COOKIE, oid, max_age=86400, samesite="lax",
                        path="/", httponly=True)
    return resp


@app.get("/pay/success", response_class=HTMLResponse)
def pay_success(o: str = "", request: Request = None, bg: BackgroundTasks = None) -> HTMLResponse:
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

    Здесь же — второй шанс на доставку: если платёж подтверждён, а плана нет, страница
    сама запускает фулфилмент. Раньше она видела succeeded, не запускала НИЧЕГО и по
    таймауту обещала письмо, которого никто не отправлял.
    """
    oid = "".join(c for c in (o or "") if c.isalnum())[:40]
    # Фолбэк на куку: страница вычищает ?o= из адреса (там тот же токен, что и у
    # плана, — см. ниже), поэтому после F5 заказ надо чем-то опознать. Кука своя
    # у каждого браузера, чужой заказ по ней не откроется.
    if not oid and request is not None:
        oid = "".join(c for c in request.cookies.get(PAY_ORDER_COOKIE, "") if c.isalnum())[:40]
    order = _find_order(oid) if oid else {}
    pid = order.get("payment_id", "")
    st = _yk_get_payment(pid).get("status", "") if pid else ""
    plan_ready = bool(oid) and (PLANS / f"{oid}.json").exists()
    pay_url = order.get("pay_url", "")

    if st == "succeeded" and oid and not plan_ready and order.get("type") != "sub_renew":
        # Деньги подтверждены, плана нет — вебхук либо не дошёл, либо упал на генерации.
        # Человек стоит на этой странице ИМЕННО СЕЙЧАС, ждать часового тика cron незачем.
        # Повторное открытие страницы дубля не даст: _fulfill_once проверяет файл плана
        # и держит аренду на время генерации.
        _bump("fulfill_retry_success_page")
        print(f"[ALERT] /pay/success: оплачено, плана нет — запускаем доставку order={oid}", flush=True)
        _base = str(request.base_url).rstrip("/") if request is not None else PUBLIC_BASE
        _args = (order.get("email", ""), order.get("quiz") or {}, oid, _base)
        if bg is not None:
            bg.add_task(_fulfill_once, *_args)
        else:  # прямой вызов (тесты) — фоновых задач нет, но доставка всё равно должна пойти
            import threading
            threading.Thread(target=_fulfill_once, args=_args, daemon=True).start()

    if st == "canceled" and not plan_ready:
        return _pay_failed_page(oid)
    if not plan_ready and st not in ("succeeded", "pending", "waiting_for_capture"):
        # Нет заказа, нет платежа или API молчит. Не выдумываем статус.
        return _pay_unknown_page()

    paid = plan_ready or st == "succeeded"

    poll = ""
    if oid:
        # Текст по таймауту раньше утверждал «отправили ссылку на почту». Письмо уходит
        # ТОЛЬКО вместе с готовым планом, а раз мы досюда дошли — плана нет, значит и
        # письма не было. Обещать его — врать. Говорим, что доделаем, и даём канал связи.
        late = ("Сборка затянулась. Мы это видим и доведём до конца — ссылка придёт на почту. "
                "Если письма не будет в течение часа, напиши: support@mynutriplan.ru"
                if paid else
                "Банк всё ещё не подтвердил платёж. Если деньги спишутся, план соберётся сам "
                "и ссылка придёт на почту. Если списание уже прошло — "
                "напиши: support@mynutriplan.ru")
        poll = (
            "<script>(function(){var t=0;"
            f"var u='/api/plan/{oid}/ready',p='/plan/{oid}';"
            "function tick(){t+=3;fetch(u).then(function(r){return r.json()}).then(function(j){"
            "if(j.ready){location.href=p;return;}"
            "if(t<240){setTimeout(tick,3000);}else{"
            # textContent, а не innerHTML: текст здесь наш, но подставлять его как разметку
            # без нужды — лишний способ однажды получить XSS
            f"document.getElementById('wait').textContent={json.dumps(late, ensure_ascii=False)};"
            "}"
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
        # С лендингом заказа, а не голый /quiz: человек должен вернуться в свою
        # воронку, а не в чужую. Плюс без ?l= квиз считал заход «с другого
        # лендинга» и стирал все 11 ответов — у того, кто уже дошёл до оплаты.
        _ls = "".join(c for c in str(order.get("landing") or "") if c.isalnum() or c in "-_")[:24]
        again = (f"<a href=\"/quiz{'?l=' + _ls if _ls else ''}\" style=\"display:block;margin-top:14px;"
                 "color:#6B7566;font-size:14px;"
                 "text-decoration:underline;text-underline-offset:3px\">Оформить заново</a>")
        actions = f"<div style=\"margin-top:22px\">{back}{again}</div>"

    # Пароль предлагаем ровно здесь: почта уже известна, деньги уже прошли, и это
    # единственный момент, когда человек точно смотрит на экран. Блок НЕ мешает
    # плану открыться — поллер выше уводит на план, как только тот готов, и
    # заполнять пароль необязательно.
    pw_block = ""
    if paid:
        pw_block = (
            "<form id='pwf' style='margin-top:26px;width:100%;max-width:340px;text-align:left'>"
            "<div style='font-weight:800;font-size:16px;margin-bottom:4px'>Придумай пароль</div>"
            "<div style='color:#6B7566;font-size:13.5px;margin-bottom:12px'>"
            "Чтобы заходить в план с любого устройства, не дожидаясь письма.</div>"
            "<input id='pw1' type='password' autocomplete='new-password' placeholder='Пароль' "
            "style='width:100%;box-sizing:border-box;border:1.5px solid #E6DECD;border-radius:12px;"
            "padding:13px 14px;font-size:16px;background:#fff'>"
            "<input id='pw2' type='password' autocomplete='new-password' placeholder='Повтори пароль' "
            "style='width:100%;box-sizing:border-box;margin-top:8px;border:1.5px solid #E6DECD;"
            "border-radius:12px;padding:13px 14px;font-size:16px;background:#fff'>"
            "<div id='pwerr' style='display:none;color:#DC2626;font-size:13px;margin-top:8px'></div>"
            "<div id='pwok' style='display:none;color:#16A34A;font-weight:700;font-size:14px;"
            "margin-top:10px'>Пароль сохранён — теперь можно входить по почте и паролю.</div>"
            "<button id='pwb' type='submit' style='width:100%;margin-top:10px;border:0;border-radius:12px;"
            "background:#16A34A;color:#fff;font-weight:800;font-size:15px;padding:14px;cursor:pointer'>"
            "Сохранить пароль</button>"
            "<div style='color:#8B9584;font-size:12px;margin-top:9px;text-align:center'>"
            "Можно пропустить — план откроется и так, ссылка придёт на почту.</div>"
            "</form>"
            "<script>(function(){var f=document.getElementById('pwf');"
            "f.addEventListener('submit',function(e){e.preventDefault();"
            "var b=document.getElementById('pwb'),er=document.getElementById('pwerr'),"
            "ok=document.getElementById('pwok');er.style.display='none';b.disabled=true;"
            "b.textContent='Сохраняю…';"
            "fetch('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({password:document.getElementById('pw1').value,"
            "password2:document.getElementById('pw2').value})})"
            ".then(function(r){return r.json()}).then(function(j){"
            "if(j.ok){ok.style.display='block';f.querySelectorAll('input,button').forEach("
            "function(x){x.disabled=true});return;}"
            "er.textContent=j.error||'Не удалось сохранить';er.style.display='block';"
            "b.disabled=false;b.textContent='Сохранить пароль';})"
            ".catch(function(){er.textContent='Нет связи — попробуй ещё раз';"
            "er.style.display='block';b.disabled=false;b.textContent='Сохранить пароль';});"
            "});})();</script>")

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

    # Адрес чистим тем же скриптом, что и экраны отказа (_PAY_URL_SCRUB — там же
    # объяснено, почему до счётчика). Заказ после этого опознаётся по куке (см.
    # начало функции), поэтому F5 не выкидывает человека на «не нашли платёж».
    scrub = _PAY_URL_SCRUB if oid else ""

    resp = HTMLResponse(_inject_metrika(
        # <head> здесь настоящий, а не для красоты: _inject_metrika вставляет
        # счётчик ПЕРЕД </head>, и без него страница возврата оставалась без
        # аналитики — слепое пятно №1 из доктрины add-payments: деньги дошли,
        # а Метрика и Директ об этом не узнали.
        # Цели вида «URL содержит /pay/success» продолжают срабатывать.
        _no_referrer_leak(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        + scrub +
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
        + actions + pw_block +
        "<div style='margin-top:18px;width:34px;height:34px;border:3px solid #DCFCE7;border-top-color:#16A34A;"
        "border-radius:50%;animation:sp 1s linear infinite'></div>"
        "<style>@keyframes sp{to{transform:rotate(360deg)}}</style>"
        + poll + paid_goal + "</div></body></html>"), anon_page="/pay/success"))
    if oid:
        # Живёт сутки: страница возврата актуальна минуты, но человек может
        # вернуться на неё из истории браузера, пока план собирается.
        # path='/', а не '/pay/success': по этой куке форма «Придумай пароль»
        # доказывает право на аккаунт, а живёт форма на той же странице, но
        # стучится в /api/auth/password. С узким path кука туда не долетала, и
        # задать пароль после оплаты не мог НИКТО — все шли через «Забыли пароль».
        resp.set_cookie(PAY_ORDER_COOKIE, oid, max_age=86400, samesite="lax",
                        path="/", httponly=True)
    return resp


def _return_mails(token: str, pl: dict, base: str, now: datetime) -> int:
    """Письма-возвраты на 2-й и 6-й день плана. По одному разу на токен: метка лежит
    в самом плане и переживает недельную регенерацию (_PLAN_STICKY), иначе каждая
    новая неделя рассылала бы их заново. Работает и без подписки — адрес берём из заказа."""
    started = pl.get("started")
    if not started:
        return 0  # планы, сохранённые до появления поля: точки отсчёта нет, задним числом не шлём
    try:
        d0 = datetime.fromisoformat(started)
        if d0.tzinfo is None:
            d0 = d0.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return 0
    age = (now - d0).days  # день старта = 0, значит 2-й день плана это age 1, 6-й — age 5
    need_d2 = age >= 1 and not pl.get("mail_d2")
    need_d6 = age >= 5 and not pl.get("mail_d6")
    if not (need_d2 or need_d6):
        return 0
    sub = _load_sub(token)
    email = _plan_email(token)
    if not email:
        return 0
    sent = 0
    if need_d2:
        _plan_mark(token, {"mail_d2": now.isoformat()})  # метка ДО отправки — не дублировать
        _send_email(email, "Как первый день? · NutriPlan", _plan_d2_email(base, token), "plan_d2")
        sent += 1
    if need_d6:
        # Автосборку обещаем, только если неделя реально придёт — и считаем это на момент
        # СЛЕДУЮЩЕЙ сборки (next_plan), а не на сегодня: у отменившего оплаченный период
        # может кончиться раньше неё, и «соберём автоматически» было бы ложью.
        when = now
        try:
            when = max(now, datetime.fromisoformat(sub["next_plan"]))
        except Exception:  # noqa: BLE001 — нет подписки или битая дата: остаёмся на now
            pass
        renews = _renews_weeks(sub, when)
        _plan_mark(token, {"mail_d6": now.isoformat()})
        _send_email(email, "Неделя заканчивается · NutriPlan", _plan_d6_email(base, token, renews), "plan_d6")
        sent += 1
    return sent


METRIKA_TOKEN = os.getenv("NUTRI_METRIKA_TOKEN", "").strip()
_YM_UPLOADED = DATA / "ym_uploaded.json"


def _upload_offline_conversions() -> dict:
    """Догрузить оплаты в Метрику офлайн-конверсиями.

    Зачем вообще: цель pay_success шлётся только с экрана возврата. Закрыл вкладку
    в приложении банка, вернулся при ещё не подтверждённом платеже, не дождался
    ответа ЮKassa — деньги пришли, план выдан, а Метрика и Директ конверсии не
    увидели. Значит, оптимизировать рекламу не по чему.

    Грузим по ClientId (кука _ym_uid), его пишет _marks при создании платежа.
    Без NUTRI_METRIKA_TOKEN тихо ничего не делаем: на локальной площадке и на
    стенде токена нет, и падать из-за этого крон не должен.
    """
    if not (METRIKA_TOKEN and NUTRI_METRIKA_ID and ORDERS.exists()):
        return {"skipped": True}
    import urllib.request
    import uuid
    try:
        with _json_locked(_YM_UPLOADED):
            done = set(_json_read(_YM_UPLOADED).keys())
        rows = []
        for line in ORDERS.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            oid = r.get("order") or ""
            # Только подтверждённые деньги и только те, у кого есть ClientId:
            # без него Метрике не к кому привязать конверсию.
            if r.get("event") != "payment.succeeded" or not oid or oid in done:
                continue
            # ClientId лежит в строке СОЗДАНИЯ заказа, а не в строке оплаты:
            # куку читает /api/pay/create, а вебхук приходит без браузера. Пока
            # искали только в строке succeeded, выгрузка была мертва — прогон на
            # боевом формате давал uploaded: 0 всегда.
            cid = (r.get("ym_uid") or "").strip() or (_find_order(oid).get("ym_uid") or "").strip()
            if not cid:
                continue
            try:
                ts = int(datetime.fromisoformat(str(r.get("ts") or "")).timestamp())
            except Exception:  # noqa: BLE001
                continue
            goal = "pay_success_sub" if r.get("type", "").startswith("sub") else "pay_success_once"
            rows.append((cid, goal, ts, str(r.get("amount") or ""), oid))
        if not rows:
            return {"uploaded": 0}
        csv = "ClientId,Target,DateTime,Price,Currency\n" + "\n".join(
            f"{c},{g},{t},{p or 0},RUB" for c, g, t, p, _ in rows)
        boundary = "----npconv" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"conv.csv\"\r\nContent-Type: text/csv\r\n\r\n{csv}\r\n"
                f"--{boundary}--\r\n").encode()
        url = (f"https://api-metrika.yandex.net/management/v1/counter/{NUTRI_METRIKA_ID}"
               f"/offline_conversions/upload?client_id_type=CLIENT_ID")
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"OAuth {METRIKA_TOKEN}",
            "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        # Помечаем ПОСЛЕ успешной загрузки: пометить раньше — значит навсегда
        # потерять конверсию, если запрос не дошёл.
        with _json_locked(_YM_UPLOADED):
            d = _json_read(_YM_UPLOADED)
            for _c, _g, _t, _p, oid in rows:
                d[oid] = datetime.now(timezone.utc).isoformat()
            if len(d) > 20000:
                d = dict(sorted(d.items(), key=lambda kv: kv[1])[-10000:])
            _json_write(_YM_UPLOADED, d)
        _bump("ym_conv_uploaded", len(rows))
        return {"uploaded": len(rows)}
    except Exception as e:  # noqa: BLE001
        _bump("ym_conv_fail")
        print(f"[ALERT] офлайн-конверсии не загрузились: {e}", flush=True)
        return {"error": str(e)[:120]}


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
    out = {"checked": 0, "replanned": 0, "replan_deferred": 0, "billed": 0, "pending": 0,
           "retrying": 0, "past_due": 0, "ended": 0, "demo_skipped": 0, "canceled_served": 0,
           "renew_notified": 0, "owed_sent": 0}
    for sub in _all_subs():
        # демо-подписки (для проверяющих ЮKassa) не биллим и не регенерим — иначе
        # cron спишет с фейковой карты, провалится и покажет демо как past_due
        if (sub.get("sub_id") in DEMO_TOKENS) or (sub.get("plan_token") in DEMO_TOKENS):
            out["demo_skipped"] += 1
            continue
        st0 = sub.get("status")
        active = st0 == "active"
        # past_due — это НЕ «отвалился», а «списание не прошло, идут ретраи». Раньше цикл
        # брал только active и оплаченный canceled, поэтому подписка, которую вебхук увёл
        # в past_due, выпадала из обслуживания НАВСЕГДА: ни повторной попытки списать, ни
        # письма «не удалось продлить», ни новых недель — а человек считал себя подписанным.
        retry = st0 == "past_due"
        # Отменивший УЖЕ ОПЛАТИЛ период до next_charge — недели ему довозим до конца
        # этого периода (деньги при этом не трогаем: списание только для active/past_due).
        # Тот же _renews_weeks, что решает, что обещать в письме 6-го дня: обещание и
        # обслуживание обязаны считаться по одному правилу, иначе они разъезжаются.
        served = (not active) and (not retry) and _renews_weeks(sub, now)
        if not (active or retry or served):
            # У отменившего кончился оплаченный период — подписка реально завершена.
            # Раньше cron просто переставал её брать: статус навсегда оставался
            # canceled, экран показывал «доступ до …» с датой в прошлом, а письма
            # ни в этот день, ни позже человек не получал вообще никакого.
            _end = _sub_until(sub)
            if st0 == "canceled" and _end and now >= _end \
                    and _guarded_status(sub["sub_id"], "ended", only=("canceled",)):
                out["ended"] += 1
                _sub_mail_once(sub["sub_id"], "mail_ended", "Подписка завершена · NutriPlan",
                               _sub_ended_email(base, "canceled"), "sub_ended")
            continue
        out["checked"] += 1
        if served:
            out["canceled_served"] += 1
        sid = sub["sub_id"]
        upd: dict = {}      # только cron-поля, пишем через merge (не затираем cancel/unbind)
        rem: list = []
        try:  # недельная регенерация плана (тот же токен → PWA показывает свежую неделю)
            if now >= datetime.fromisoformat(sub["next_plan"]):
                token = sub["plan_token"]
                old = _load_plan(token)
                # avoid заведён ровно против повторов, а недельная сборка его не передавала:
                # при том же квизе выходила БУКВАЛЬНО та же неделя — за 499 ₽/мес.
                pl = plan_ai.generate_plan(sub.get("quiz") or {},
                                           avoid=list(dict.fromkeys(_menu_names(old))))
                pl["quiz"] = sub.get("quiz") or {}
                same = _menu_names(pl) == _menu_names(old)
                # Сброс «приготовил» оправдан только сменой меню. При недоступном LLM
                # банк-заготовка детерминирована и повторяет прошлую неделю — сбрасывать
                # там отметки значит стереть человеку неделю и ничего не дать взамен.
                _save_plan(token, pl, reset_progress=not same)
                link = f"{base}/plan/{token}"
                if pl.get("source") != "ai":
                    # Письмо «Новый план на неделю» по банк-заготовке — обещание того,
                    # чего в плане нет. Ставим долг: письмо уйдёт из цикла самолечения
                    # ниже, как только соберётся полноценная новая неделя.
                    _plan_mark(token, {"mail_owed": "weekly"})
                    out["replan_deferred"] += 1
                elif same:
                    out["replan_deferred"] += 1  # меню то же — рекламировать нечего
                else:
                    # Фото — ДО письма. Клиент опрашивает готовность 6 раз с шагом 5 секунд
                    # и сдаётся, дальше на экране остаётся svg-тарелка. При оплате прогрев
                    # есть, в недельной регенерации его просто забыли. Кэш общий по слагу,
                    # так что греются только новые блюда недели.
                    _pregen_dish_photos(pl)
                    _send_email(sub["email"], "Новый план на неделю · NutriPlan",
                                plan.menu_email_html(pl, link), "plan_weekly")
                upd["next_plan"] = (datetime.fromisoformat(sub["next_plan"]) + timedelta(days=7)).isoformat()
                out["replanned"] += 1
        except Exception:  # noqa: BLE001
            pass
        # деньги — у active и у past_due (там как раз и идут ретраи); с отменённого не
        # списываем, ему лишь довозим недели до конца оплаченного периода
        billable = active or retry
        try:  # месячное списание (pending-aware + grace-ретраи + честная обработка отвязки)
            pend = sub.get("pending_charge_id") if billable else None
            nc = datetime.fromisoformat(sub["next_charge"])
            due = billable and now >= nc
            # Предупреждение за 3 дня. Только если списание реально произойдёт (карта на
            # месте) и ровно один раз на период — метка хранит тот next_charge, о котором
            # уже предупредили, поэтому в следующем месяце предупредим снова.
            if (active and sub.get("payment_method_id") and not pend and not due
                    and nc - now <= timedelta(days=3)
                    and sub.get("renew_notified_for") != sub["next_charge"]):
                _sub_merge(sid, {"renew_notified_for": sub["next_charge"]})  # метка ДО отправки
                _send_email(sub["email"], "Через 3 дня продлим подписку · NutriPlan",
                            _sub_renew_soon_email(base, sid, sub.get("amount", SUB_PRICE_RUB),
                                                  sub["next_charge"]), "sub_renew_soon")
                out["renew_notified"] += 1
            if pend:
                st = _yk_payment_status(pend)  # досматриваем незакрытый платёж, новый НЕ создаём
                if st == "succeeded":
                    if _advance_charge(sub, now):
                        upd["next_charge"] = sub["next_charge"]; out["billed"] += 1
                    rem.append("pending_charge_id")
                    if retry:  # ретрай дозрел до успеха — возвращаем подписку в строй
                        _guarded_status(sid, "active", only=("past_due",))
                    _mail_charged(sub, pend, base)
                elif st == "canceled":
                    rem.append("pending_charge_id")  # провал → grace-логика на след. тике
                # иначе pending — ждём
            elif due and not sub.get("payment_method_id"):
                # Карта отвязана юзером (unbind) — автопродление невозможно. По истечении
                # периода честно ЗАВЕРШАЕМ подписку. НЕ «банк отклонил», НЕ grace-ретраи.
                if _guarded_status(sid, "ended", only=("active", "past_due")):
                    out["ended"] += 1
                    # Через общий _sub_mail_once, как и остальные письма по подписке:
                    # ключ mail_ended один на все три причины завершения, второго
                    # «подписка завершена» человек не получит ни по какой ветке.
                    _sub_mail_once(sid, "mail_ended", "Подписка завершена · NutriPlan",
                                   _sub_ended_email(base, "unbound"), "sub_ended")
            elif due:
                status, pid = _charge_subscription(sub)
                if status == "succeeded":
                    if _advance_charge(sub, now):
                        upd["next_charge"] = sub["next_charge"]
                    out["billed"] += 1
                    if retry:  # деньги пришли — подписка снова здорова (но не воскрешаем отменённую)
                        _guarded_status(sid, "active", only=("past_due",))
                    _mail_charged(sub, pid, base)
                elif status == "pending":
                    upd["pending_charge_id"] = pid; out["pending"] += 1  # НЕ провал
                else:  # failed — ретраим до next_charge+SUB_GRACE, по исчерпании окна ended + письмо
                    if now > nc + SUB_GRACE:
                        # Конечное состояние — ended, а НЕ past_due: past_due теперь означает
                        # «ретраим», и оставлять её в нём значило бы дёргать мёртвую карту вечно.
                        if _guarded_status(sid, "ended", only=("active", "past_due")):
                            out["ended"] += 1
                            _sub_mail_once(sid, "mail_ended", "Не удалось продлить подписку · NutriPlan",
                                           _sub_ended_email(base, "failed"), "sub_past_due")
                    else:
                        # Первый провал метим past_due: это видно в ЛК и в данных, а cron
                        # продолжает ретраить (past_due остаётся в обслуживании).
                        if _guarded_status(sid, "past_due"):
                            out["past_due"] += 1
                        out["retrying"] += 1
        except Exception:  # noqa: BLE001
            pass
        if upd or rem:
            _sub_merge(sid, upd, remove=tuple(rem))
    # Один проход по планам: письма-возвраты + самолечение деградированных.
    # Кап на тик: у починки — LLM-затраты, у писем — чтобы разовый сбой не вылился
    # в рассылку по всей базе за один тик.
    out["repaired"] = 0
    out["returned"] = 0
    for f in sorted(PLANS.glob("*.json")):
        try:
            if f.stem in DEMO_TOKENS:
                continue
            pl = json.loads(f.read_text(encoding="utf-8"))
            if out["returned"] < 20:
                out["returned"] += _return_mails(f.stem, pl, base, now)
            # source != "ai" (или явная метка) — план без рецептов и списка покупок
            if out["repaired"] < 3 and (pl.get("source") != "ai" or pl.get("degraded")) and pl.get("quiz"):
                fresh = plan_ai.generate_plan(pl["quiz"])
                if fresh.get("source") == "ai":  # апгрейд только на полноценный план
                    fresh["quiz"] = pl["quiz"]
                    # reset_progress: чинёный план — ДРУГОЙ набор блюд, старые отметки
                    # «приготовил» повисли бы на чужих блюдах.
                    # Метка degraded не переносится → снимается тут.
                    _save_plan(f.stem, fresh, reset_progress=True)
                    _pregen_dish_photos(fresh)   # блюда сменились — иначе на экране плейсхолдеры
                    _bump("plan_repaired")
                    out["repaired"] += 1
                    # Долг по письму: пока план был заготовкой, человеку ушла честная
                    # заглушка («дособираем»), а письмо с меню откладывалось до этого
                    # момента. Гасим долг в любом случае — иначе висящая метка при
                    # ненайденной почте заставляла бы слать его каждый тик.
                    owed = _OWED_MAIL.get(pl.get("mail_owed") or "")
                    if owed:
                        em = _plan_email(f.stem)
                        if em:
                            _send_email(em, owed[0],
                                        plan.menu_email_html(fresh, f"{base}/plan/{f.stem}"), owed[1])
                            out["owed_sent"] += 1
                        _plan_mark(f.stem, {"mail_owed": ""})
            # Долг висит дольше часа, а план всё ещё заготовка (LLM недоступна или
            # квиза нет вовсе). Раньше в этой ветке человек не получал НИЧЕГО:
            # письмо с меню ждало апгрейда, которого могло не случиться никогда, а
            # обещание «пришлём в течение часа» истекало молча. Отдаём то, что
            # есть, — заготовка хуже полноценного плана, но бесконечно лучше
            # тишины после оплаты.
            elif pl.get("mail_owed") and out["owed_sent"] < 10:
                age = _days_since(pl.get("started") or "")
                started_h = None
                try:
                    started_h = (now - datetime.fromisoformat(str(pl.get("started")))).total_seconds() / 3600
                except Exception:  # noqa: BLE001
                    started_h = 24 if age else None
                if started_h is not None and started_h >= 1:
                    owed = _OWED_MAIL.get(pl.get("mail_owed") or "")
                    em = _plan_email(f.stem)
                    if owed and em:
                        _send_email(em, owed[0],
                                    plan.menu_email_html(pl, f"{base}/plan/{f.stem}"), owed[1])
                        out["owed_sent"] += 1
                        _bump("owed_sent_degraded")
                        print(f"[ALERT] отдали заготовку по долгу письма: {f.stem}", flush=True)
                    _plan_mark(f.stem, {"mail_owed": ""})
        except Exception:  # noqa: BLE001
            pass
    out["fulfilled"] = _sweep_unfulfilled(now, base)
    # Догрузка конверсий — в конце и в try: реклама важна, но не важнее того,
    # чтобы тик крона довёл до конца биллинг и выдачу недель.
    try:
        out["ym_conv"] = _upload_offline_conversions()
    except Exception as e:  # noqa: BLE001
        out["ym_conv"] = {"error": str(e)[:80]}
    return JSONResponse(out)


def _sweep_unfulfilled(now: datetime, base: str, cap: int = 5) -> int:
    """Сверка журнала заказов с планами: оплата есть, файла плана нет — дозываем доставку.

    Прошлый цикл самолечения ходил по PLANS.glob, то есть чинил только УЖЕ СУЩЕСТВУЮЩИЕ
    планы. Заказ, у которого плана не появилось вовсе (упавшая генерация, рестарт
    контейнера, потерянный вебхук), не видел никто: деньги взяли, план не отдали, и в
    системе об этом не было ни строки.

    Окно: старше 10 минут (иначе догоняем нормальную генерацию, которая ещё идёт) и не
    старше суток (древние заказы уже разобраны руками, повторная рассылка навредит).
    Кап на тик — у генерации LLM-цена."""
    done = 0
    try:
        paid: dict[str, str] = {}   # order → время подтверждения оплаты (последняя запись побеждает)
        for line in ORDERS.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue  # битая строка не должна обрывать сверку
            oid = rec.get("order") or ""
            if not oid:
                continue
            if rec.get("status") == "succeeded":
                paid[oid] = rec.get("ts") or ""
            elif rec.get("status") == "refunded":
                paid.pop(oid, None)  # деньги вернули — доставлять нечего
    except Exception:  # noqa: BLE001
        return 0
    for oid, ts in paid.items():
        if done >= cap:
            break
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        if not (timedelta(minutes=10) <= now - t <= timedelta(days=1)):
            continue
        if oid in DEMO_TOKENS or not _plan_missing(oid):
            continue
        order = _find_order(oid)
        _bump("fulfill_rescued")
        print(f"[ALERT] оплачено без плана — дозаказываем: order={oid} "
              f"email={order.get('email', '?')}", flush=True)
        if _fulfill_once(order.get("email", ""), order.get("quiz") or {}, oid, base):
            done += 1
    return done


@app.get("/sub/cancel", response_class=HTMLResponse)
def sub_cancel(s: str = "") -> HTMLResponse:
    """GET безопасен (не мутирует!) — отмена только через POST /api/sub/{token}/cancel с
    двойным подтверждением в ЛК. Раньше GET отменял подписку побочным эффектом (любой
    префетч/сканер по публичной ссылке мог отменить)."""
    tok = "".join(c for c in (s or "") if c.isalnum())
    if tok and _load_sub(tok):
        # Ведём на план С ЯВНЫМ ПРИЗНАКОМ, что открывать надо окно подписки. Раньше
        # был только хэш `#sub`, а его никто не разбирал: ссылка «управление подпиской»
        # из каждого письма приземляла человека на обычный экран «Сегодня», и отмена
        # оставалась достижимой лишь прокруткой ~1160 px до серой ссылки в подвале.
        # Отдаём оба сигнала: `?sub=1` переживает редиректы и переоткрытие из истории,
        # хэш остаётся для совместимости со старыми письмами.
        return RedirectResponse(f"/plan/{tok}?sub=1#sub", status_code=302)
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
    # Деградация плана — вопрос выполнения обязательства (пейволл продавал рецепты и
    # список покупок), поэтому владелец должен видеть её здесь, а не только в логе.
    open_deg = []
    try:
        for f in sorted(PLANS.glob("*.json")):
            if len(open_deg) >= 50:
                break
            pl = json.loads(f.read_text(encoding="utf-8"))
            if pl.get("degraded"):
                open_deg.append({"token": f.stem, "at": pl.get("degraded_at", ""),
                                 "source": pl.get("source", ""), "ver": pl.get("ver")})
    except Exception:  # noqa: BLE001
        pass
    return JSONResponse({"landings": rows, "degraded": {
        "open": open_deg,                                   # не починенные прямо сейчас
        "hits": c.get("plan_degraded", 0),                  # сколько раз вообще случалось
        "sold": c.get("fulfill_degraded", 0),               # из них — ушло оплатившим
        "repaired": c.get("plan_repaired", 0),              # дорегенерировано cron'ом
    }, "pay_health": {
        # Технические сбои оплаты. Нужны, чтобы «ноль продаж» читалось однозначно:
        # ноль при нулях здесь — воронка, ноль при ненулях — лежащая касса.
        "create_error": c.get("pay_create_error", 0),       # разовый платёж не создан
        "sub_create_error": c.get("sub_create_error", 0),   # подписка не создана
        "webhook_verify_fail": c.get("webhook_verify_fail", 0),  # не смогли перепроверить платёж
        "webhook_error": c.get("webhook_error", 0),         # исключение в обработчике
        "fulfill_fail": c.get("fulfill_fail", 0),           # оплата есть, доставка упала
    }})
