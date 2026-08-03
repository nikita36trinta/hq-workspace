"""Переиспользуемый пайплайн продуктовой аналитики (вендорится в каждый проект).

Автономный, конфиг-driven модуль: приём событий + локальное хранилище (jsonl) +
агрегация (воронка, денежная петля, отвал-по-секундам, трение формы, объект→лифт,
LTV/AOV, NET-маржа и CB-rate по варианту/методу оплаты) + встроенный дашборд.

Подключение в проект (3 строки):
    from analytics import make_router, AnalyticsConfig
    app.include_router(make_router(ANALYTICS_CFG))
    # + <script src="/static/analytics.js"> + window.ANALYTICS_CONFIG во фронте

Никаких проектных импортов. Данные о платежах (для денег/LTV/NET) проект отдаёт
через колбэк payments_provider (по одному интерфейсу — см. AnalyticsConfig).
Разработано под best-practices аудит: меряем NET, а не gross; видим пост-оплатный
слой; тегируем вариант/метод/источник. Всё опционально — не задал, не считается.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

_KAD_RE = re.compile(r"^\s*\d{2}:\d{1,2}:\d{1,7}:\d{1,7}\s*$")
_BOT_SIGS = ("bot", "crawl", "spider", "slurp", "headless", "python-requests",
             "curl", "wget", "monitor", "preview", "facebookexternalhit")


@dataclass
class AnalyticsConfig:
    """Конфиг на проект. Обязательны: project_id, data_dir, admin_token, funnel."""
    project_id: str
    data_dir: str
    admin_token: str
    title: str = "Аналитика"
    # Воронка: [(имя_события, метка)] строго по убыванию (каждый шаг ⊆ предыдущего).
    funnel: list[tuple[str, str]] = field(default_factory=list)
    # Денежная петля: [(имя_события, метка)] клик → шлюз → оплата.
    money: list[tuple[str, str]] = field(default_factory=list)
    # Спец-роли событий (какое событие несёт какую нагрузку):
    #   obj_split — поле 'obj' (address/cadastre), abandon — 'sec' (финальный beacon ухода),
    #   friction — 'fields', lift — 'shown', bump — 'bump' (апселл),
    #   completed — событие «дождался результата» (кто НЕ ушёл; для реконструкции отвала).
    roles: dict[str, str] = field(default_factory=dict)
    # Heartbeat: имя события пинга «я ещё тут» (несёт 'sec'). Клиент шлёт каждые N сек,
    # пока экран загрузки виден; сервер берёт max(sec) как last-seen → секунда ухода даже
    # когда финальный beacon не дошёл (мобильный дроп / отошёл-не-закрыл). См. analytics.js.
    heartbeat_event: str = ""
    price_variants: dict[str, int] | None = None       # для A/B и NET-маржи
    payments_provider: Callable[[], list[dict]] | None = None
    since_provider: Callable[[], str | None] | None = None
    cogs_provider: Callable[[], dict[str, float]] | None = None  # order_id → себестоимость
    # Проектные добавки в дашборд (модуль остаётся агностичным — только рендерит):
    #   extra_kpis_provider(ctx) → [{"label","value","sub","tone"}] — плитки в верхнюю сводку.
    #   panels_provider(ctx)     → [панель] — произвольные карточки (см. render._render_panel).
    #   ctx = {"src": <источник трафика>, "token": <admin-token>} на каждый запрос дашборда.
    extra_kpis_provider: Callable[[dict], list[dict]] | None = None
    panels_provider: Callable[[dict], list[dict]] | None = None
    extra_events: set[str] = field(default_factory=set)  # доп-события проекта
    # Прочие сигналы: событие → человекочитаемая метка. Показываются отдельной
    # карточкой (счётчик уников) — чтобы ничто собираемое не оставалось невидимым.
    signal_labels: dict[str, str] = field(default_factory=dict)
    cookie: str = "an_sid"                               # имя сессионной куки
    # Кука атрибуции кампании (напр. "cs_camp"): при приёме события штампуем его этой
    # меткой → весь дашборд можно фильтровать/сравнивать по кампании (матрица). Пусто = выкл.
    campaign_cookie: str = ""
    # Кампании, полностью исключаемые из статистики (метка липкая, поэтому даже после
    # паузы кампании её first-touch посетители продолжают капать — их вон отовсюду).
    exclude_campaigns: set[str] = field(default_factory=set)
    # Кука «это свой, не считать»: владелец и разработчик ходят по бою чаще любого
    # клиента, и их клики ломают ровно те числа, ради которых аналитика и заводилась —
    # конверсию, воронку, плечи A/B. Событие с этой кукой отбрасывается на приёме, то
    # есть в журнал не попадает вовсе и задним числом статистику не портит. Пусто = выкл.
    exclude_cookie: str = ""
    # ---- срезы события (кто/откуда), без них воронка — одно число без объяснения ----
    # Устройство/ОС/браузер из User-Agent. UA приходит в каждом запросе, и раньше мы его
    # читали только чтобы отсеять ботов, после чего выбрасывали — поэтому «мобильный
    # конвертит вдвое хуже» узнавалось лишь из Метрики, где нет ни наших денег, ни A/B.
    stamp_device: bool = False
    # Куки → поля события: {"cs_src": "src", "cs_v2": "ab"}. Источник и вариант теста
    # лежат в куках с первого захода, но на события не попадали — из-за этого воронку
    # нельзя было разложить ни на рекламу/органику, ни на плечи A/B.
    stamp_cookies: dict[str, str] = field(default_factory=dict)
    # Проектный хук: (имя события, тело запроса) → доп-поля. Нужен там, где срез знает
    # только проект (напр. регион объекта из кадастрового номера).
    derive_fields: Callable[[str, dict], dict] | None = None

    def allowed(self) -> set[str]:
        names = {n for n, _ in self.funnel} | {n for n, _ in self.money}
        names |= set(self.roles.values()) | set(self.extra_events)
        names |= set(self.signal_labels)
        if self.heartbeat_event:
            names.add(self.heartbeat_event)
        return names

    def validate(self) -> dict[str, list[str]]:
        """Проверка корректности интеграции. errors → make_router упадёт; warnings → лог.
        Ловит типовые ошибки подключения ДО того, как они станут тихими багами."""
        errors, warnings = [], []
        if not self.project_id:
            errors.append("project_id пуст")
        if not self.data_dir:
            errors.append("data_dir пуст (некуда писать события)")
        if not self.admin_token:
            warnings.append("admin_token пуст → /admin/* всегда 403 (дашборд недоступен)")
        if not self.funnel:
            errors.append("funnel пуст — воронка не построится")
        for i, step in enumerate(self.funnel):
            if not (isinstance(step, (tuple, list)) and len(step) == 2):
                errors.append(f"funnel[{i}] не (имя, метка): {step!r}")
        for i, step in enumerate(self.money):
            if not (isinstance(step, (tuple, list)) and len(step) == 2):
                errors.append(f"money[{i}] не (имя, метка): {step!r}")
        # роли: только известные ключи (значения авто-добавляются в allowed() и всегда принимаются)
        for role in self.roles:
            if role not in ("obj_split", "abandon", "friction", "lift", "bump", "completed"):
                warnings.append(f"неизвестная роль '{role}' (игнорируется)")
        if self.price_variants:
            for k, v in self.price_variants.items():
                if not isinstance(v, (int, float)):
                    errors.append(f"price_variants['{k}']={v!r} не число")
        for name in ("payments_provider", "since_provider", "cogs_provider",
                     "extra_kpis_provider", "panels_provider", "derive_fields"):
            fn = getattr(self, name)
            if fn is not None and not callable(fn):
                errors.append(f"{name} задан, но не callable")
        try:
            p = Path(self.data_dir)
            p.mkdir(parents=True, exist_ok=True)
            t = p / ".an_write_test"
            t.write_text("x", encoding="utf-8")
            t.unlink()
        except Exception as e:  # noqa: BLE001
            errors.append(f"data_dir не пишется: {self.data_dir} ({e})")
        return {"errors": errors, "warnings": warnings}


def classify_object(ref: str) -> str:
    """Тип объекта по строке: cadastre / address / other. Вынесено для тестируемости."""
    ref = (ref or "").strip()
    return "cadastre" if _KAD_RE.match(ref) else ("address" if len(ref) >= 3 else "other")


# ---------- хранилище ----------

def _events_path(cfg: AnalyticsConfig) -> Path:
    return Path(cfg.data_dir) / "analytics_events.jsonl"


def _is_bot(request: Request) -> bool:
    ua = (request.headers.get("user-agent") or "").lower()
    return (not ua) or any(s in ua for s in _BOT_SIGS)


# ---------- срезы из User-Agent ----------
# Намеренно маленький разбор без сторонних библиотек: нам нужны не точные версии,
# а сегменты, по которым режется воронка. Порядок проверок значим — планшет ищем
# раньше телефона, встроенные браузеры раньше Chrome/Safari (они все притворяются
# и Chrome, и Safari сразу, поэтому «первое совпадение выигрывает»).

_BROWSERS = (
    ("YaBrowser", "Яндекс.Браузер"), ("YaSearchBrowser", "Яндекс (приложение)"),
    ("YaApp", "Яндекс (приложение)"), ("YandexSearch", "Яндекс (приложение)"),
    ("MiuiBrowser", "MIUI"), ("SamsungBrowser", "Samsung Internet"),
    ("HuaweiBrowser", "Huawei"), ("OPR/", "Opera"), ("Opera", "Opera"),
    ("Edg", "Edge"), ("FxiOS", "Firefox"), ("Firefox", "Firefox"),
    ("CriOS", "Chrome"), ("Chrome", "Chrome"), ("Safari", "Safari"),
)


def ua_dims(ua: str) -> dict[str, str]:
    """User-Agent → {dev, os, br}. Пустой UA сюда не доходит (отсекается как бот)."""
    u = ua or ""
    low = u.lower()

    if "ipad" in low or ("android" in low and "mobile" not in low) or "tablet" in low:
        dev = "планшет"
    elif ("mobi" in low or "iphone" in low or "ipod" in low or "android" in low
          or "phone" in low):
        dev = "смартфон"
    else:
        dev = "десктоп"

    if "android" in low:
        os_name = "Android"
    elif "iphone" in low or "ipad" in low or "ipod" in low or "ios" in low:
        os_name = "iOS"
    elif "windows" in low:
        os_name = "Windows"
    elif "mac os" in low or "macintosh" in low:
        os_name = "macOS"
    elif "linux" in low:
        os_name = "Linux"
    else:
        os_name = "прочее"

    br = "прочее"
    for needle, label in _BROWSERS:
        if needle.lower() in low:
            br = label
            break
    return {"dev": dev, "os": os_name, "br": br}


def _log_event(cfg: AnalyticsConfig, rec: dict) -> None:
    try:
        p = _events_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — аналитика не должна ронять запрос
        pass


def _since(cfg: AnalyticsConfig) -> str | None:
    if cfg.since_provider:
        try:
            return cfg.since_provider()
        except Exception:  # noqa: BLE001
            return None
    return None


# ---------- агрегация ----------

_CAMP_ORGANIC = "прямой/органика"


def build_dashboard(cfg: AnalyticsConfig, ctx: dict | None = None) -> dict[str, Any]:
    """Собрать все метрики из лога событий + платежей (по since).
    ctx (опц.): {src, token, campaign}. campaign≠"all" → весь дашборд фильтруется на
    эту кампанию; матрица кампаний считается всегда (сравнение всех бок о бок)."""
    ctx = ctx or {}
    since = _since(cfg)
    roles = cfg.roles
    funnel_names = [n for n, _ in cfg.funnel]
    money_names = [n for n, _ in cfg.money]

    seen: dict[str, set] = {n: set() for n in funnel_names}
    money_seen: dict[str, set] = {n: set() for n in money_names}
    obj_split: dict[str, set] = {"address": set(), "cadastre": set(), "other": set()}
    # отвал на загрузке: секунда ухода = max(явный beacon, last-seen heartbeat) по сессии
    abandon_sec: dict[str, int] = {}     # sid → sec финального beacon ухода
    hb_sec: dict[str, int] = {}          # sid → max sec heartbeat (last-seen)
    hb_ts: dict[str, str] = {}           # sid → ts последнего heartbeat (staleness-guard)
    completed_sids: set = set()          # sid, кто дождался (НЕ ушёл)
    hb_event = cfg.heartbeat_event
    completed_ev = roles.get("completed")
    friction: dict[str, int] = {}
    obj_lift: dict[str, int] = {}          # sid → shown 0/1
    bump_yes = bump_total = 0
    pay_click_sids: set = set()
    signal_seen: dict[str, set] = {n: set() for n in cfg.signal_labels}  # прочие сигналы (уники)

    camp_filter = (ctx.get("campaign") or "all")
    exclude_camps = cfg.exclude_campaigns or set()
    matrix_on = bool(cfg.campaign_cookie)
    sid_camp: dict[str, str] = {}                 # sid → кампания сессии (приоритет не-органика)
    matrix_seen: dict[str, dict[str, set]] = {}   # camp → {шаг воронки: set(sid)}

    p = _events_path(cfg)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    if matrix_on:  # пред-проход: кампания каждой сессии (кука cs_camp липкая, но visit до неё без метки)
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if since and (r.get("ts") or "") < since:
                continue
            sid, camp = r.get("sid") or "", r.get("camp") or ""
            if not sid:
                continue
            if camp and (sid not in sid_camp or sid_camp[sid] == _CAMP_ORGANIC):
                sid_camp[sid] = camp
            elif sid not in sid_camp:
                sid_camp[sid] = _CAMP_ORGANIC
    if lines:
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if since and (r.get("ts") or "") < since:
                continue
            nm, sid = r.get("name"), r.get("sid") or ""
            if not sid:
                continue
            scamp = sid_camp.get(sid, _CAMP_ORGANIC)
            if scamp in exclude_camps:                # исключённая кампания (напр. РСЯ) — вон отовсюду
                continue
            if matrix_on and nm in seen:              # матрица: воронка по кампании (ДО фильтра)
                matrix_seen.setdefault(scamp, {}).setdefault(nm, set()).add(sid)
            if matrix_on and camp_filter != "all" and scamp != camp_filter:
                continue                              # фильтр: весь дашборд на выбранную кампанию
            if nm in seen:
                seen[nm].add(sid)
            if nm in money_seen:
                money_seen[nm].add(sid)
            if nm == roles.get("obj_split"):
                obj_split.setdefault(r.get("obj") or "other", set()).add(sid)
            if nm == roles.get("abandon") and isinstance(r.get("sec"), int):
                if r["sec"] > abandon_sec.get(sid, -1):
                    abandon_sec[sid] = r["sec"]
            if hb_event and nm == hb_event and isinstance(r.get("sec"), int):
                if r["sec"] > hb_sec.get(sid, -1):
                    hb_sec[sid] = r["sec"]
                hb_ts[sid] = r.get("ts") or hb_ts.get(sid)
            if completed_ev and nm == completed_ev:
                completed_sids.add(sid)
            if nm == roles.get("friction"):
                key = r.get("fields") or "none"
                friction[key] = friction.get(key, 0) + 1
            if nm == roles.get("lift"):
                obj_lift[sid] = 1 if r.get("shown") else 0
            if nm == roles.get("bump"):
                bump_total += 1
                bump_yes += 1 if r.get("bump") else 0
            if nm == (cfg.money[0][0] if cfg.money else None):
                pay_click_sids.add(sid)
            if nm in signal_seen:
                signal_seen[nm].add(sid)

    visits = len(seen[funnel_names[0]]) if funnel_names else 0
    vbase = visits or 1

    fmap = dict(cfg.funnel)
    steps, prev = [], None
    for n in funnel_names:
        c = len(seen.get(n) or set())
        steps.append({"name": n, "label": fmap[n], "count": c,
                      "pct_base": c / vbase if vbase else 0,
                      "pct_prev": (c / prev) if (prev and prev > 0) else None,
                      "obj_here": (n == roles.get("obj_split"))})
        prev = c

    # отвал на загрузке: реконструкция секунды ухода (heartbeat last-seen + финальный beacon)
    abandon = _reconstruct_abandon(abandon_sec, hb_sec, hb_ts, completed_sids)

    # денежная петля + оплаты
    payments = []
    if cfg.payments_provider:
        try:
            payments = cfg.payments_provider() or []
        except Exception:  # noqa: BLE001
            payments = []
    paid = [x for x in payments if x.get("status") == "succeeded"
            and (not since or (x.get("ts") or x.get("paid_at") or "") >= since)]
    if exclude_camps:  # исключённые кампании — вон и из денег/матрицы
        paid = [x for x in paid if (x.get("campaign") or _CAMP_ORGANIC) not in exclude_camps]

    # матрица кампаний: воронка (по вехам) + оплаты/выручка на каждую кампанию (по ВСЕМ, до фильтра)
    campaign_matrix = None
    if matrix_on:
        pay_by_camp: dict[str, dict] = {}
        for x in paid:
            c = x.get("campaign") or _CAMP_ORGANIC
            a = pay_by_camp.setdefault(c, {"orders": 0, "rev": 0.0})
            a["orders"] += 1
            a["rev"] += float(x.get("amount") or 0)
        camps = set(matrix_seen) | set(pay_by_camp)
        mrows = []
        for c in sorted(camps):
            ms = matrix_seen.get(c, {})
            v = len(ms.get(funnel_names[0], set())) if funnel_names else 0
            pc = pay_by_camp.get(c, {})
            orders, rev = pc.get("orders", 0), round(pc.get("rev", 0.0))
            mrows.append({"camp": c, "visits": v,
                          "steps": {n: len(ms.get(n, set())) for n in funnel_names},
                          "paid": orders, "revenue": rev,
                          "cvr": (orders / v) if v else 0.0,
                          "aov": round(rev / orders) if orders else 0})
        mrows.sort(key=lambda x: (-x["revenue"], -x["visits"]))
        campaign_matrix = {"rows": mrows, "funnel": [(n, fmap[n]) for n in funnel_names],
                           "selected": camp_filter}
        # основной дашборд: выбрана кампания → платежи тоже фильтруем на неё
        if camp_filter != "all":
            paid = [x for x in paid if (x.get("campaign") or _CAMP_ORGANIC) == camp_filter]

    money = _money_stats(cfg, money_seen, paid, bump_yes, bump_total)

    # объект→конверсия (лифт)
    lift = _lift_stats(obj_lift, pay_click_sids)

    # LTV / повторные + NET-маржа по варианту
    ltv = _ltv_stats(paid)
    net = _net_by_variant(cfg, paid)

    # проектные добавки (A/B, ops-метрики и пр.) — модуль лишь рендерит их
    extra_kpis, panels = [], []
    if cfg.extra_kpis_provider:
        try:
            extra_kpis = cfg.extra_kpis_provider(ctx) or []
        except Exception:  # noqa: BLE001
            extra_kpis = []
    if cfg.panels_provider:
        try:
            panels = cfg.panels_provider(ctx) or []
        except Exception:  # noqa: BLE001
            panels = []

    return {
        "project_id": cfg.project_id, "since": since, "visits": visits,
        "steps": steps, "obj": {k: len(v) for k, v in obj_split.items()},
        "abandon": abandon, "abandon_tracked": bool(cfg.roles.get("abandon")),
        "money": money, "lift": lift,
        "friction": sorted(friction.items(), key=lambda kv: -kv[1])[:8],
        "ltv": ltv, "net": net,
        "signals": [{"name": n, "label": cfg.signal_labels[n], "count": len(signal_seen[n])}
                    for n in cfg.signal_labels],
        "campaign_matrix": campaign_matrix, "campaign_selected": camp_filter,
        "extra_kpis": extra_kpis, "panels": panels,
    }


def _parse_ts(s: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


# Свежий heartbeat (сессия, возможно, ещё грузится) → не считаем ушедшей раньше времени.
_INFLIGHT_SEC = 180


def _reconstruct_abandon(abandon_sec: dict, hb_sec: dict, hb_ts: dict,
                         completed: set) -> dict | None:
    """Секунда ухода на сессию = max(явный beacon ухода, last-seen heartbeat).
    Кто дождался (completed) — исключаем. Сессию только с heartbeat, чей последний пинг
    свежее _INFLIGHT_SEC, пропускаем (может ещё грузиться, а не ушла)."""
    now = datetime.now(timezone.utc)
    secs: list[int] = []
    for sid in set(abandon_sec) | set(hb_sec):
        if sid in completed:
            continue
        explicit = abandon_sec.get(sid)
        last = hb_sec.get(sid)
        if explicit is None and last is not None:
            ts = _parse_ts(hb_ts.get(sid))
            if ts and (now - ts).total_seconds() < _INFLIGHT_SEC:
                continue  # ещё «в полёте» — рано записывать в ушедшие
        sec = max(explicit if explicit is not None else -1,
                  last if last is not None else -1)
        if sec >= 0:
            secs.append(sec)
    return _abandon_stats(secs)


def _abandon_stats(secs: list[int]) -> dict | None:
    if not secs:
        return None
    ss = sorted(secs)
    buckets = [("0–15с", 0, 15), ("15–30с", 15, 30), ("30–45с", 30, 45),
               ("45–60с", 45, 60), ("60–90с", 60, 90), ("90с+", 90, 10 ** 9)]
    dist = [(lbl, sum(1 for x in ss if lo <= x < hi)) for lbl, lo, hi in buckets]
    return {"n": len(ss), "median": ss[len(ss) // 2], "avg": round(sum(ss) / len(ss)),
            "max_bucket": max(dist, key=lambda b: b[1])[0], "dist": dist}


def _money_stats(cfg, money_seen, paid, bump_yes, bump_total) -> dict:
    steps = [{"label": lbl, "count": len(money_seen.get(n) or set())} for n, lbl in cfg.money]
    # добавим финальный шаг «оплатили» из платежей, если его нет в событиях
    paid_n = len(paid)
    counts = [s["count"] for s in steps] + [paid_n]
    labels = [s["label"] for s in steps] + ["Оплатили (факт)"]
    rows, prev = [], None
    for lbl, c in zip(labels, counts):
        drop = (1 - c / prev) if (prev and prev > 0) else None
        rows.append({"label": lbl, "count": c, "drop": drop})
        prev = c
    # метод оплаты берём из ФАКТИЧЕСКИХ платежей (там он есть), а не из событий —
    # событие оплаты метод не несёт. Раньше читалось из событий → всегда пусто.
    method_mix: dict[str, int] = {}
    for p in paid:
        m = p.get("method") or p.get("payment_method")
        if m:
            method_mix[m] = method_mix.get(m, 0) + 1
    return {"rows": rows, "method_mix": method_mix,
            "bump_yes": bump_yes, "bump_total": bump_total,
            "bump_rate": (bump_yes / bump_total) if bump_total else None}


def _lift_stats(obj_lift, pay_click_sids) -> dict | None:
    if not obj_lift:
        return None
    def conv(sids):
        n = len(sids)
        c = sum(1 for s in sids if s in pay_click_sids)
        return {"n": n, "conv": c, "rate": (c / n) if n else 0.0}
    return {"with": conv([s for s, v in obj_lift.items() if v]),
            "without": conv([s for s, v in obj_lift.items() if not v])}


def _ltv_stats(paid) -> dict | None:
    if not paid:
        return None
    by_email: dict[str, int] = {}
    amounts = []
    for x in paid:
        em = (x.get("email") or "").strip().lower()
        if em:
            by_email[em] = by_email.get(em, 0) + 1
        try:
            amounts.append(float(x.get("amount") or 0))
        except Exception:  # noqa: BLE001
            pass
    repeat = sum(1 for v in by_email.values() if v > 1)
    return {"buyers": len(by_email), "orders": len(paid), "repeat": repeat,
            "repeat_rate": (repeat / len(by_email)) if by_email else None,
            "aov": round(sum(amounts) / len(amounts)) if amounts else None}


def _net_by_variant(cfg, paid) -> list | None:
    """NET-маржа по ценовому варианту: gross − возвраты − CB − комиссия − COGS."""
    if not cfg.price_variants or not paid:
        return None
    cogs = {}
    if cfg.cogs_provider:
        try:
            cogs = cfg.cogs_provider() or {}
        except Exception:  # noqa: BLE001
            cogs = {}
    agg: dict[str, dict] = {}
    for x in paid:
        v = x.get("variant") or "?"
        a = agg.setdefault(v, {"orders": 0, "gross": 0.0, "refund": 0.0, "cb": 0.0, "fee": 0.0, "cogs": 0.0})
        amt = float(x.get("amount") or 0)
        a["orders"] += 1
        a["gross"] += amt
        if x.get("refunded"):
            a["refund"] += float(x.get("refund_amount") or amt)
        if x.get("chargeback"):
            a["cb"] += float(x.get("cb_amount") or amt)
        a["fee"] += float(x.get("fee") or amt * 0.035)  # дефолт комиссии ~3.5%
        a["cogs"] += float(cogs.get(x.get("order") or x.get("order_id") or "", 0))
    rows = []
    for v, a in sorted(agg.items()):
        net = a["gross"] - a["refund"] - a["cb"] - a["fee"] - a["cogs"]
        cbrate = (a["cb"] / a["gross"]) if a["gross"] else 0
        rows.append({"variant": v, "price": cfg.price_variants.get(v),
                     "orders": a["orders"], "gross": round(a["gross"]),
                     "net": round(net), "cb_rate": cbrate,
                     "net_per_order": round(net / a["orders"]) if a["orders"] else 0})
    return rows


# ---------- роутер ----------

# Имена, которые фронт слал, а сервер отбросил (нет в cfg.allowed()). Отброс тихий —
# ручка отвечает 200 OK и ничего не пишет, поэтому событие можно слать месяцами и не
# заметить пропажи. Так на sdelka в никуда улетали bump_yes/bump_no, email_left и вся
# страница отчёта. Держим в памяти (не в файле): это диагностика подключения, а не
# данные — переживать рестарт ей незачем.
_REJECTED: dict[str, dict[str, int]] = {}
_REJECTED_CAP = 40          # защита от мусорных/ботовых имён — больше в память не берём


def _note_rejected(project_id: str, name: str) -> None:
    box = _REJECTED.setdefault(project_id, {})
    if name in box:
        box[name] += 1
    elif len(box) < _REJECTED_CAP:
        box[name] = 1


def selfcheck(cfg: AnalyticsConfig) -> dict[str, Any]:
    """Диагностика интеграции: конфиг + хранилище + какие события реально приходят.
    Хит после подключения в проект: сразу видно, всё ли правильно имплементировано."""
    v = cfg.validate()
    p = _events_path(cfg)
    seen_events: dict[str, int] = {}
    sessions: set = set()
    last_ts = None
    total = 0
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            total += 1
            nm = r.get("name")
            seen_events[nm] = seen_events.get(nm, 0) + 1
            if r.get("sid"):
                sessions.add(r["sid"])
            last_ts = r.get("ts") or last_ts
    # каждое объявленное событие: приходит ли (иначе фронт не подключён в этой точке).
    # Дедуп по имени события; роль дописывается меткой.
    labels: dict[str, str] = {}
    for nm, label in list(cfg.funnel) + list(cfg.money):
        labels.setdefault(nm, label)
    for role, ev in cfg.roles.items():
        if ev:
            labels[ev] = (labels.get(ev, "") + f" [роль:{role}]").strip()
    wiring = [{"event": nm, "label": labels[nm], "seen": seen_events.get(nm, 0)}
              for nm in sorted(labels, key=lambda n: -seen_events.get(n, 0))]
    rejected = sorted(_REJECTED.get(cfg.project_id, {}).items(), key=lambda kv: -kv[1])
    ok = not v["errors"] and not rejected
    if v["errors"]:
        hint = "Есть ошибки конфига — см. config.errors."
    elif rejected:
        names = ", ".join(n for n, _ in rejected[:6])
        hint = (f"Фронт шлёт события, которых нет в конфиге, и они молча теряются: {names}. "
                f"Добавь их в extra_events/signal_labels — или убери вызов на фронте.")
    else:
        hint = "Всё ок."
    return {"ok": ok, "project_id": cfg.project_id, "config": v,
            "storage": {"path": str(p), "exists": p.exists(), "events": total,
                        "sessions": len(sessions), "last_ts": last_ts},
            "wiring": wiring,
            # события, отброшенные с момента последнего рестарта
            "rejected": [{"event": n, "count": c} for n, c in rejected],
            "hint": hint}


def make_router(cfg: AnalyticsConfig) -> APIRouter:
    v = cfg.validate()
    if v["errors"]:
        raise ValueError("Analytics config errors: " + "; ".join(v["errors"]))
    for w in v["warnings"]:
        print(f"[analytics:{cfg.project_id}] warning: {w}")
    router = APIRouter()
    allowed = cfg.allowed()
    obj_split_ev = cfg.roles.get("obj_split")

    @router.post("/api/goal")
    async def ingest(request: Request) -> JSONResponse:
        if _is_bot(request):
            return JSONResponse({"ok": True})
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        # Свой трафик выбрасываем ДО всего остального: ни журнала, ни счётчика
        # отброшенных имён — как будто визита не было.
        if cfg.exclude_cookie and request.cookies.get(cfg.exclude_cookie):
            return JSONResponse({"ok": True, "skipped": "notrack"})
        name = (body or {}).get("name", "")
        if name not in allowed:
            _note_rejected(cfg.project_id, str(name)[:60])
            return JSONResponse({"ok": True})
        # сквозной id: device_id (localStorage) приоритетнее сессионной куки
        did = str((body or {}).get("did", ""))[:40]
        sid = did or request.cookies.get(cfg.cookie) or ""
        new_sid = ""
        if not sid:
            sid = new_sid = uuid.uuid4().hex[:16]
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "sid": sid, "name": name}
        if cfg.campaign_cookie:  # атрибуция кампании (для матрицы/фильтра)
            camp = request.cookies.get(cfg.campaign_cookie, "")
            if camp:
                rec["camp"] = str(camp)[:60]
        b = body or {}
        if name == obj_split_ev:
            rec["obj"] = classify_object(str(b.get("object", "")))
        # Срезы «кто и откуда». Пишем на КАЖДОЕ событие, а не только на визит: сессия
        # может начаться до деплоя или потеряться (приватный режим, чищеный localStorage),
        # и тогда разложить по срезу удалось бы только часть воронки — то есть неверно.
        if cfg.stamp_device:
            rec.update(ua_dims(request.headers.get("user-agent") or ""))
        for cookie_name, fieldname in (cfg.stamp_cookies or {}).items():
            v = request.cookies.get(cookie_name, "")
            if v:
                rec[fieldname] = str(v)[:40]
        if cfg.derive_fields:
            try:
                for k, v in (cfg.derive_fields(name, b) or {}).items():
                    if v:
                        rec[str(k)[:12]] = str(v)[:60]
            except Exception:  # noqa: BLE001 — срез не имеет права ронять приём события
                pass
        # ref — источник перехода (только хост, без пути и параметров: путь чужого сайта
        # нам не нужен, а параметры могут нести чужие ПДн). err — текст JS-ошибки,
        # part — какой блок страницы увидели.
        for k in ("sec", "bump", "shown", "fields", "method", "order", "ref", "err", "part"):
            if k in b:
                rec[k] = (max(0, min(600, int(b[k]))) if k == "sec"
                          else 1 if (k in ("bump", "shown") and b[k]) else 0 if k in ("bump", "shown")
                          else str(b[k])[:60] if k != "err" else str(b[k])[:200])
        _log_event(cfg, rec)
        resp = JSONResponse({"ok": True})
        if new_sid:
            resp.set_cookie(cfg.cookie, new_sid, max_age=60 * 60 * 24 * 90, samesite="lax")
        return resp

    @router.get("/admin/stats")
    def dashboard(token: str = "", src: str = "all", campaign: str = "all") -> Response:
        if not cfg.admin_token or token != cfg.admin_token:
            raise HTTPException(status_code=403, detail="forbidden")
        ctx = {"src": src, "token": token, "campaign": campaign}
        data = build_dashboard(cfg, ctx)
        data["selfcheck"] = selfcheck(cfg)   # состояние подключения — на той же странице
        data["campaign_token"] = token       # для ссылок фильтра матрицы
        return HTMLResponse(render_dashboard(cfg, data))

    @router.get("/admin/stats.json")
    def dashboard_json(token: str = "", src: str = "all", campaign: str = "all") -> JSONResponse:
        if not cfg.admin_token or token != cfg.admin_token:
            raise HTTPException(status_code=403, detail="forbidden")
        return JSONResponse(build_dashboard(cfg, {"src": src, "token": token, "campaign": campaign}))

    @router.get("/admin/selfcheck")
    def selfcheck_route(token: str = "") -> JSONResponse:
        if not cfg.admin_token or token != cfg.admin_token:
            raise HTTPException(status_code=403, detail="forbidden")
        return JSONResponse(selfcheck(cfg))

    return router


# рендер вынесен в отдельный модуль для чистоты (работает и как submodule пакета, и standalone)
try:
    from .analytics_render import render_dashboard  # noqa: E402
except ImportError:  # standalone / не-пакет
    from analytics_render import render_dashboard  # noqa: E402
