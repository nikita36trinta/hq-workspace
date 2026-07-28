"""Проверочные тесты ядра аналитики. Запуск: pytest test_analytics.py -q

Покрывают: валидацию конфига, классификацию объекта, агрегацию (воронка, отвал,
денежная петля, NET-маржа, лифт, LTV, трение), self-check. Чистая логика, без сети
и async — быстрые и портируемые (проверяют, что ядро имплементировано верно)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analytics as an  # noqa: E402


def _cfg(tmp_path, **kw):
    base = dict(
        project_id="t", data_dir=str(tmp_path), admin_token="secret", title="T",
        funnel=[("visit", "Визит"), ("checkout_open", "Форма"), ("check_started", "Запуск"),
                ("check_completed", "Дождался"), ("pay_click", "Оплатить")],
        money=[("pay_click", "Клик"), ("yookassa_reached", "ЮKassa")],
        roles={"obj_split": "check_started", "abandon": "check_abandoned",
               "friction": "checkout_abandon", "lift": "preview_object", "bump": "pay_click"},
        extra_events={"check_abandoned", "checkout_abandon", "preview_object", "yookassa_reached"},
    )
    base.update(kw)
    return an.AnalyticsConfig(**base)


def _write(cfg, events):
    p = an._events_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


# ---------- классификация объекта ----------

def test_classify_object():
    assert an.classify_object("77:01:0004012:1122") == "cadastre"
    assert an.classify_object("г Москва, Ленинский 30") == "address"
    assert an.classify_object("аб") == "other"
    assert an.classify_object("") == "other"


# ---------- валидация конфига ----------

def test_validate_ok(tmp_path):
    assert _cfg(tmp_path).validate()["errors"] == []


def test_validate_empty_funnel(tmp_path):
    assert _cfg(tmp_path, funnel=[]).validate()["errors"]


def test_validate_unknown_role_warns(tmp_path):
    c = _cfg(tmp_path, roles={"bogus_role": "x", "obj_split": "check_started"})
    assert any("bogus_role" in w for w in c.validate()["warnings"])


def test_validate_admin_token_empty_warns(tmp_path):
    assert _cfg(tmp_path, admin_token="").validate()["warnings"]


def test_make_router_raises_on_bad_config(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        an.make_router(_cfg(tmp_path, funnel=[]))


# ---------- агрегация: воронка + сплит объекта ----------

def test_funnel_and_obj_split(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, [
        {"ts": "2z", "sid": "a", "name": "visit"},
        {"ts": "2z", "sid": "a", "name": "checkout_open"},
        {"ts": "2z", "sid": "a", "name": "check_started", "obj": "cadastre"},
        {"ts": "2z", "sid": "b", "name": "visit"},
        {"ts": "2z", "sid": "b", "name": "check_started", "obj": "address"},
    ])
    d = an.build_dashboard(cfg)
    assert d["visits"] == 2
    steps = {s["name"]: s["count"] for s in d["steps"]}
    assert steps["visit"] == 2 and steps["checkout_open"] == 1 and steps["check_started"] == 2
    assert d["obj"]["cadastre"] == 1 and d["obj"]["address"] == 1


def test_dedup_same_session(tmp_path):
    """Один sid считается один раз на шаг (уники, не события)."""
    cfg = _cfg(tmp_path)
    _write(cfg, [{"ts": "2z", "sid": "a", "name": "visit"}] * 3)
    assert an.build_dashboard(cfg)["visits"] == 1


# ---------- отвал по секундам ----------

def test_abandon_stats(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, [{"ts": "2z", "sid": str(i), "name": "check_abandoned", "sec": s}
                 for i, s in enumerate([12, 20, 28, 40, 70])])
    ab = an.build_dashboard(cfg)["abandon"]
    assert ab["n"] == 5 and ab["median"] == 28
    assert dict(ab["dist"])["15–30с"] == 2


def test_abandon_reconstruct_from_heartbeat(tmp_path):
    """Секунда ухода из heartbeat last-seen: max(явный beacon, последний пинг);
    дождавшихся исключаем; свежий heartbeat (ещё грузится) не считаем."""
    from datetime import datetime, timezone
    recent = datetime.now(timezone.utc).isoformat()
    cfg = _cfg(tmp_path, heartbeat_event="check_heartbeat",
               roles={"abandon": "check_abandoned", "completed": "check_completed"})
    _write(cfg, [
        # A: пинги до 15с, потом тишина, не дождался → ушёл на ~15 (last-seen)
        {"ts": "1z", "sid": "A", "name": "check_heartbeat", "sec": 5},
        {"ts": "1z", "sid": "A", "name": "check_heartbeat", "sec": 10},
        {"ts": "1z", "sid": "A", "name": "check_heartbeat", "sec": 15},
        # B: пинги + дождался результата → НЕ ушёл (исключаем)
        {"ts": "1z", "sid": "B", "name": "check_heartbeat", "sec": 5},
        {"ts": "1z", "sid": "B", "name": "check_completed"},
        # C: heartbeat 20 + финальный beacon 22 → max = 22
        {"ts": "1z", "sid": "C", "name": "check_heartbeat", "sec": 20},
        {"ts": "1z", "sid": "C", "name": "check_abandoned", "sec": 22},
        # D: только heartbeat, СВЕЖИЙ ts → сессия ещё грузится, не считаем
        {"ts": recent, "sid": "D", "name": "check_heartbeat", "sec": 8},
    ])
    ab = an.build_dashboard(cfg)["abandon"]
    assert ab["n"] == 2          # A(15) и C(22); B дождался, D в полёте
    assert ab["median"] == 22    # sorted [15, 22] → индекс 1


# ---------- денежная петля + bump ----------

def test_money_loop_and_bump(tmp_path):
    cfg = _cfg(tmp_path, payments_provider=lambda: [
        {"status": "succeeded", "amount": 490, "email": "a@b.c", "ts": "3z", "variant": "B"}])
    _write(cfg, [
        {"ts": "3z", "sid": "a", "name": "pay_click", "bump": 1},
        {"ts": "3z", "sid": "b", "name": "pay_click", "bump": 0},
        {"ts": "3z", "sid": "a", "name": "yookassa_reached"},
    ])
    m = an.build_dashboard(cfg)["money"]
    labels = {r["label"]: r["count"] for r in m["rows"]}
    assert labels["Клик"] == 2 and labels["ЮKassa"] == 1
    assert labels["Оплатили (факт)"] == 1
    assert m["bump_yes"] == 1 and m["bump_total"] == 2 and m["bump_rate"] == 0.5


# ---------- метод оплаты берётся из платежей, не из событий ----------

def test_method_mix_from_payments(tmp_path):
    cfg = _cfg(tmp_path, payments_provider=lambda: [
        {"status": "succeeded", "amount": 490, "ts": "3z", "method": "sbp"},
        {"status": "succeeded", "amount": 490, "ts": "3z", "payment_method": "bank_card"},
        {"status": "succeeded", "amount": 490, "ts": "3z", "method": "sbp"}])
    _write(cfg, [{"ts": "3z", "sid": "a", "name": "pay_click"}])  # событие метод НЕ несёт
    mix = an.build_dashboard(cfg)["money"]["method_mix"]
    assert mix == {"sbp": 2, "bank_card": 1}


# ---------- прочие сигналы (ничто собираемое не невидимо) ----------

def test_signals_counted_and_rendered(tmp_path):
    cfg = _cfg(tmp_path, signal_labels={"cta_click": "Клик по CTA", "bump_shown": "Показ апселла"})
    assert "cta_click" in cfg.allowed() and "bump_shown" in cfg.allowed()  # принимаются ingest
    _write(cfg, [
        {"ts": "9z", "sid": "a", "name": "cta_click"},
        {"ts": "9z", "sid": "a", "name": "cta_click"},   # тот же sid — уник
        {"ts": "9z", "sid": "b", "name": "cta_click"},
        {"ts": "9z", "sid": "a", "name": "bump_shown"}])
    sig = {s["name"]: s["count"] for s in an.build_dashboard(cfg)["signals"]}
    assert sig["cta_click"] == 2 and sig["bump_shown"] == 1
    from analytics_render import render_dashboard
    html = render_dashboard(cfg, an.build_dashboard(cfg))
    assert "Клик по CTA" in html and "Показ апселла" in html


# ---------- NET-маржа по варианту ----------

def test_net_margin_subtracts_costs(tmp_path):
    cfg = _cfg(tmp_path, price_variants={"B": 490, "D": 5299},
               payments_provider=lambda: [
                   {"status": "succeeded", "amount": 490, "variant": "B", "order": "o1", "ts": "4z"},
                   {"status": "succeeded", "amount": 5299, "variant": "D", "order": "o2", "ts": "4z",
                    "chargeback": True, "cb_amount": 5299, "fee": 185},
               ],
               cogs_provider=lambda: {"o1": 50, "o2": 50})
    net = {r["variant"]: r for r in an.build_dashboard(cfg)["net"]}
    # B: 490 − fee(490*0.035≈17.15) − cogs 50 ≈ 423
    assert net["B"]["net"] < 490 and net["B"]["net"] > 400
    # D: чарджбэк съедает всё → net сильно отрицательный, cb_rate = 100%
    assert net["D"]["net"] < 0 and net["D"]["cb_rate"] == 1.0


# ---------- объект → лифт ----------

def test_object_lift(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, [
        {"ts": "5z", "sid": "a", "name": "preview_object", "shown": 1},
        {"ts": "5z", "sid": "a", "name": "pay_click"},
        {"ts": "5z", "sid": "b", "name": "preview_object", "shown": 0},
    ])
    lf = an.build_dashboard(cfg)["lift"]
    assert lf["with"]["n"] == 1 and lf["with"]["conv"] == 1 and lf["with"]["rate"] == 1.0
    assert lf["without"]["n"] == 1 and lf["without"]["conv"] == 0


# ---------- LTV / повторные ----------

def test_ltv_repeat_buyers(tmp_path):
    cfg = _cfg(tmp_path, payments_provider=lambda: [
        {"status": "succeeded", "amount": 490, "email": "x@x", "ts": "6z"},
        {"status": "succeeded", "amount": 690, "email": "x@x", "ts": "6z"},
        {"status": "succeeded", "amount": 299, "email": "y@y", "ts": "6z"},
    ])
    lt = an.build_dashboard(cfg)["ltv"]
    assert lt["buyers"] == 2 and lt["orders"] == 3 and lt["repeat"] == 1
    assert lt["aov"] == round((490 + 690 + 299) / 3)


# ---------- трение формы ----------

def test_friction(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, [
        {"ts": "7z", "sid": "a", "name": "checkout_abandon", "fields": "fio"},
        {"ts": "7z", "sid": "b", "name": "checkout_abandon", "fields": "fio"},
        {"ts": "7z", "sid": "c", "name": "checkout_abandon", "fields": "none"},
    ])
    fr = dict(an.build_dashboard(cfg)["friction"])
    assert fr["fio"] == 2 and fr["none"] == 1


# ---------- since фильтрует старое ----------

def test_since_filters_old(tmp_path):
    cfg = _cfg(tmp_path, since_provider=lambda: "2026-07-21T00:00:00")
    _write(cfg, [
        {"ts": "2026-07-20T10:00:00", "sid": "old", "name": "visit"},
        {"ts": "2026-07-21T10:00:00", "sid": "new", "name": "visit"},
    ])
    assert an.build_dashboard(cfg)["visits"] == 1


# ---------- проектные панели / доп-KPI ----------

def test_panels_and_kpis_injected(tmp_path):
    cfg = _cfg(tmp_path,
               extra_kpis_provider=lambda ctx: [{"label": "Баланс", "value": "100 ₽", "tone": "bad"}],
               panels_provider=lambda ctx: [{"title": f"A/B ({ctx.get('src')})", "table": {"head": ["V"], "rows": [["A"]]}}])
    _write(cfg, [{"ts": "9z", "sid": "a", "name": "visit"}])
    d = an.build_dashboard(cfg, {"src": "ad", "token": "t"})
    assert d["extra_kpis"][0]["label"] == "Баланс"
    assert d["panels"][0]["title"] == "A/B (ad)"          # ctx долетел до провайдера
    from analytics_render import render_dashboard
    html = render_dashboard(cfg, d)
    assert "A/B (ad)" in html and "Баланс" in html         # отрендерилось на странице


def test_panels_provider_error_isolated(tmp_path):
    """Падение провайдера панелей не роняет дашборд."""
    def boom(ctx):
        raise RuntimeError("x")
    cfg = _cfg(tmp_path, panels_provider=boom)
    _write(cfg, [{"ts": "9z", "sid": "a", "name": "visit"}])
    d = an.build_dashboard(cfg)
    assert d["panels"] == [] and d["visits"] == 1


# ---------- матрица кампаний + drill-down фильтр ----------

def test_campaign_matrix_and_filter(tmp_path):
    cfg = _cfg(tmp_path, campaign_cookie="cs_camp",
               payments_provider=lambda: [
                   {"status": "succeeded", "amount": 498, "campaign": "sdelka_hot", "ts": "9z"},
                   {"status": "succeeded", "amount": 299, "campaign": "sdelka_rsya", "ts": "9z"}])
    _write(cfg, [
        # кампания hot: 2 визита, 1 дошёл до pay_click
        {"ts": "9z", "sid": "a", "name": "visit", "camp": "sdelka_hot"},
        {"ts": "9z", "sid": "a", "name": "pay_click", "camp": "sdelka_hot"},
        {"ts": "9z", "sid": "b", "name": "visit", "camp": "sdelka_hot"},
        # кампания rsya: 1 визит
        {"ts": "9z", "sid": "c", "name": "visit", "camp": "sdelka_rsya"},
        # органика (без camp): 1 визит
        {"ts": "9z", "sid": "d", "name": "visit"},
    ])
    d = an.build_dashboard(cfg, {"campaign": "all"})
    mx = {r["camp"]: r for r in d["campaign_matrix"]["rows"]}
    assert mx["sdelka_hot"]["visits"] == 2 and mx["sdelka_hot"]["paid"] == 1 and mx["sdelka_hot"]["revenue"] == 498
    assert mx["sdelka_rsya"]["visits"] == 1
    assert mx["прямой/органика"]["visits"] == 1
    # drill-down: фильтр на hot → визитов только 2 (не 4), выручка только hot
    df = an.build_dashboard(cfg, {"campaign": "sdelka_hot"})
    assert df["visits"] == 2
    assert df["ltv"]["orders"] == 1  # только оплата hot попала


def test_exclude_campaigns(tmp_path):
    """Исключённая кампания (напр. РСЯ) вычищается из матрицы, воронки и денег."""
    cfg = _cfg(tmp_path, campaign_cookie="cs_camp", exclude_campaigns={"sdelka_rsya"},
               payments_provider=lambda: [
                   {"status": "succeeded", "amount": 498, "campaign": "sdelka_hot", "ts": "9z"},
                   {"status": "succeeded", "amount": 299, "campaign": "sdelka_rsya", "ts": "9z"}])
    _write(cfg, [
        {"ts": "9z", "sid": "a", "name": "visit", "camp": "sdelka_hot"},
        {"ts": "9z", "sid": "r", "name": "visit", "camp": "sdelka_rsya"},
        {"ts": "9z", "sid": "r", "name": "pay_click", "camp": "sdelka_rsya"},
    ])
    d = an.build_dashboard(cfg, {"campaign": "all"})
    camps = {r["camp"] for r in d["campaign_matrix"]["rows"]}
    assert "sdelka_rsya" not in camps          # нет в матрице
    assert "sdelka_hot" in camps
    assert d["visits"] == 1                     # только hot-визит (rsya исключён)
    assert d["ltv"]["orders"] == 1 and d["ltv"]["aov"] == 498  # только оплата hot


# ---------- self-check ----------

def test_selfcheck_reports_wiring(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, [{"ts": "8z", "sid": "a", "name": "visit"}])
    sc = an.selfcheck(cfg)
    assert sc["ok"] is True
    assert sc["storage"]["events"] == 1 and sc["storage"]["sessions"] == 1
    wiring = {w["event"]: w["seen"] for w in sc["wiring"]}
    assert wiring["visit"] == 1 and wiring["pay_click"] == 0  # pay_click ещё не приходил


# ---------- срезы из User-Agent ----------

def test_ua_dims_реальные_строки():
    """Сегменты, по которым режется воронка. Строки — из боевой Метрики."""
    cases = [
        ("Mozilla/5.0 (Linux; Android 13; SM-A536B) AppleWebKit/537.36 Chrome/120 "
         "Mobile Safari/537.36 YaBrowser/23.11", "смартфон", "Android", "Яндекс.Браузер"),
        ("Mozilla/5.0 (Linux; arm_64; Android 12; RMX3085) AppleWebKit/537.36 Chrome/117 "
         "YaApp_Android/23.100 YaSearchBrowser/23.100 Mobile Safari/537.36",
         "смартфон", "Android", "Яндекс (приложение)"),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 "
         "Version/17.1 Mobile/15E148 Safari/604.1", "смартфон", "iOS", "Safari"),
        ("Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 Version/16.6 "
         "Mobile/15E148 Safari/604.1", "планшет", "iOS", "Safari"),
        # Android без «Mobile» — это планшет: у телефона токен Mobile есть всегда
        ("Mozilla/5.0 (Linux; Android 11; SM-T500) AppleWebKit/537.36 Chrome/119 Safari/537.36",
         "планшет", "Android", "Chrome"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 "
         "Safari/537.36 Edg/120.0", "десктоп", "Windows", "Edge"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
         "Version/17.0 Safari/605.1.15", "десктоп", "macOS", "Safari"),
    ]
    for ua, dev, os_name, br in cases:
        got = an.ua_dims(ua)
        assert (got["dev"], got["os"], got["br"]) == (dev, os_name, br), ua[:60]


def test_ua_dims_порядок_важен():
    """Встроенные браузеры притворяются и Chrome, и Safari сразу — выигрывает первое
    совпадение по списку, иначе весь Яндекс.Браузер схлопнется в Chrome."""
    ya = ("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120 Safari/537.36 "
          "YaBrowser/23.11")
    assert an.ua_dims(ya)["br"] == "Яндекс.Браузер"
    assert an.ua_dims("Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537.36")["br"] == "Chrome"


def test_отброшенное_событие_видно_в_selfcheck(tmp_path):
    """Имя вне конфига теряется молча (200 OK, ни строки в журнале). Раньше это
    можно было не замечать месяцами — теперь selfcheck называет пропажу по имени."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    an._REJECTED.clear()
    cfg = _cfg(tmp_path, project_id="rejtest")
    app = FastAPI()
    app.include_router(an.make_router(cfg))
    c = TestClient(app)
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537.36"}
    c.post("/api/goal", json={"name": "visit", "did": "s1"}, headers=ua)          # известное
    c.post("/api/goal", json={"name": "bump_yes", "did": "s1"}, headers=ua)       # НЕизвестное
    c.post("/api/goal", json={"name": "bump_yes", "did": "s2"}, headers=ua)

    sc = an.selfcheck(cfg)
    rej = {r["event"]: r["count"] for r in sc["rejected"]}
    assert rej == {"bump_yes": 2}, rej
    assert sc["ok"] is False                      # потеря событий — это НЕ «всё ок»
    assert "bump_yes" in sc["hint"]
    assert sc["storage"]["events"] == 1           # в журнал попал только visit


def test_отброс_не_съедает_память_на_мусорных_именах(tmp_path):
    """Бот может слать случайные имена пачками — счётчик ограничен по числу ключей."""
    an._REJECTED.clear()
    for i in range(an._REJECTED_CAP + 25):
        an._note_rejected("cap", f"мусор{i}")
    assert len(an._REJECTED["cap"]) == an._REJECTED_CAP


def test_свой_трафик_не_попадает_в_журнал(tmp_path):
    """Владелец ходит по бою чаще любого клиента. С кукой notrack его события
    отбрасываются на приёме — ни в журнале, ни среди «потерянных»."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    an._REJECTED.clear()
    cfg = _cfg(tmp_path, project_id="notrack", exclude_cookie="cs_notrack")
    app = FastAPI()
    app.include_router(an.make_router(cfg))
    c = TestClient(app)
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537.36"}

    c.post("/api/goal", json={"name": "visit", "did": "клиент"}, headers=ua)
    c.post("/api/goal", json={"name": "visit", "did": "свой"}, headers=ua,
           cookies={"cs_notrack": "1"})
    c.post("/api/goal", json={"name": "выдуманное", "did": "свой"}, headers=ua,
           cookies={"cs_notrack": "1"})

    sc = an.selfcheck(cfg)
    assert sc["storage"]["events"] == 1, "в журнал должен попасть только клиент"
    assert sc["rejected"] == [], "своё незаявленное событие не должно шуметь в диагностике"
