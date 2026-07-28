"""HTML-рендер дашборда аналитики — одностраничный современный UI.

Всё на одной странице: сводка KPI сверху, воронка-герой, остальное в сетке карточек,
плюс состояние подключения (self-check) — без открытия отдельных эндпоинтов.
Отделён от логики; без внешних зависимостей (инлайн CSS, SVG-иконки, без emoji)."""
from __future__ import annotations

from html import escape as _esc
from typing import Any
from urllib.parse import quote as _q

_CSS = """
/* ── Палитра. Светлая — ровно те цвета, что были захардкожены раньше, поэтому
   вид не изменился ни на пиксель. Тёмная — отдельный набор, а не инверсия:
   инверсия делает зелёное ядовитым, а красное — розовым. ────────────────── */
:root{
  --bg:#f5f6f8;
  --card:#fff;
  --card-2:#f8f9fb;
  --hover:#fbfcfd;
  --soft:#f4f5f8;
  --track:#eef0f4;
  --line:#ebedf2;
  --line-2:#d9dde5;
  --line-3:#b9c0cc;
  --ink:#0f172a;
  --ink-2:#334155;
  --ink-3:#4a5568;
  --mut:#5b6472;
  --mut-2:#7a828f;
  --mut-3:#9aa2af;
  --ok:#16a34a;
  --ok-d:#0f7a34;
  --bad:#c22626;
  --bad-d:#a92626;
  --warn:#b45309;
  --warn-d:#8a5a12;
  --warn-2:#e6902b;
  --accent:#3056d3;
  --accent-2:#5b7cff;
  --info-d:#2c3e66;
  --ok-bg:#eef8f1;
  --ok-bg-2:#e2f5e9;
  --bad-bg:#fceceb;
  --warn-bg:#fdf4e7;
  --info-bg:#eef2fb;
  --bg-blur:rgba(245,246,248,.82);
  --sh:#0f172a08; --sh-2:#0f172a12; --ok-ring:#16a34a22;
  color-scheme:light;
}
:root[data-theme="dark"]{
  --bg:#0e1116;
  --card:#161a21;
  --card-2:#1a1f27;
  --hover:#1d222b;
  --soft:#12161d;
  --track:#232a35;
  --line:#242b36;
  --line-2:#2e3644;
  --line-3:#3c4657;
  --ink:#e8ecf3;
  --ink-2:#c5ccd9;
  --ink-3:#aeb7c6;
  --mut:#9aa3b2;
  --mut-2:#8b94a3;
  --mut-3:#828b9a;
  --ok:#34d399;
  --ok-d:#4ade80;
  --bad:#f87171;
  --bad-d:#fca5a5;
  --warn:#fbbf24;
  --warn-d:#fcd34d;
  --warn-2:#f59e0b;
  --accent:#6b8cff;
  --accent-2:#8aa3ff;
  --info-d:#a8bdf0;
  --ok-bg:#12291d;
  --ok-bg-2:#16321f;
  --bad-bg:#2c1517;
  --warn-bg:#2b2011;
  --info-bg:#16203a;
  --bg-blur:rgba(14,17,22,.82);
  --sh:#00000055; --sh-2:#00000070; --ok-ring:#34d39933;
  color-scheme:dark;
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,sans-serif;
  margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
a{color:inherit}
.top{position:sticky;top:0;z-index:20;background:var(--bg-blur);
  backdrop-filter:saturate(180%) blur(10px);border-bottom:1px solid var(--line)}
.top-in{max-width:1120px;margin:0 auto;padding:13px 22px;display:flex;
  align-items:center;justify-content:space-between;gap:16px}
.brand{display:flex;align-items:center;gap:11px;min-width:0}
.brand h1{font-size:15.5px;font-weight:640;margin:0;letter-spacing:-.01em;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.brand .mark{width:26px;height:26px;border-radius:8px;flex:none;color:var(--card);
  background:linear-gradient(135deg,var(--accent),var(--accent-2));display:grid;place-items:center}
.brand .mark svg{width:15px;height:15px}
.meta{font-size:12.5px;color:var(--mut);display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.live{display:inline-flex;align-items:center;gap:6px}
.live b{color:var(--ink);font-weight:620}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px var(--ok-ring)}
.btn{font:inherit;font-size:12.5px;border:1px solid var(--line-2);background:var(--card);color:var(--ink-2);
  padding:6px 12px;border-radius:8px;cursor:pointer;text-decoration:none;line-height:1;
  display:inline-flex;align-items:center;gap:6px;transition:.12s}
.btn:hover{border-color:var(--line-3);background:var(--hover)}
.btn svg{width:13px;height:13px}
/* Показываем ту иконку, которая обозначает, КУДА переключим, а не текущее состояние. */
.ic-moon{display:none}
:root[data-theme="dark"] .ic-sun{display:none}
:root[data-theme="dark"] .ic-moon{display:inline-block}
.wrap{max-width:1120px;margin:0 auto;padding:24px 22px 72px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:13px;margin-bottom:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:16px 18px;
  box-shadow:0 1px 2px var(--sh)}
.kpi .l{font-size:11px;color:var(--mut-2);text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.kpi .v{font-size:27px;font-weight:680;letter-spacing:-.025em;margin-top:6px;
  font-variant-numeric:tabular-nums;line-height:1.05}
.kpi .v small{font-size:15px;font-weight:600;color:var(--mut-3);margin-left:1px}
.kpi .s{font-size:12px;color:var(--mut-2);margin-top:7px;font-variant-numeric:tabular-nums}
.kpi.good .v{color:var(--ok-d)}.kpi.bad .v{color:var(--bad)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:780px){.grid{grid-template-columns:1fr}.top-in,.wrap{padding-left:16px;padding-right:16px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:19px 21px;
  box-shadow:0 1px 2px var(--sh)}
.card.full{grid-column:1/-1}
.card h2{font-size:12px;font-weight:650;margin:0 0 15px;text-transform:uppercase;
  letter-spacing:.06em;color:var(--ink-2);display:flex;align-items:center;gap:9px}
.card h2 .ic{width:15px;height:15px;color:var(--mut-3);flex:none}
.card h2 .tag{margin-left:auto;font-size:11px;font-weight:600;letter-spacing:.02em;
  text-transform:none;color:var(--mut-2)}
.hint{color:var(--mut-2);font-size:12px;margin-top:13px;line-height:1.5}
/* funnel */
.frow{margin:0 0 15px}
.frow:last-child{margin-bottom:2px}
.fhead{display:flex;align-items:baseline;gap:10px;margin-bottom:6px}
.flabel{font-weight:580;font-size:13.5px}
.fval{font-weight:700;font-size:18px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.fpct{color:var(--mut-2);font-size:12px;font-variant-numeric:tabular-nums}
.bar{height:9px;background:var(--track);border-radius:99px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:99px;background:var(--accent);
  transition:width .5s cubic-bezier(.4,0,.2,1)}
.bar.amber>i{background:var(--warn-2)}
.fobj{font-size:12px;color:var(--mut-2);margin-top:5px;font-variant-numeric:tabular-nums}
.fobj b{color:var(--ink-2)}
.pill{font-size:11px;padding:1.5px 8px;border-radius:99px;font-weight:640;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.pill.g{background:var(--ok-bg);color:var(--ok-d)}.pill.y{background:var(--warn-bg);color:var(--warn)}
.pill.r{background:var(--bad-bg);color:var(--bad)}
/* generic rows */
.kv{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
  padding:8px 0;border-bottom:1px solid var(--soft);font-size:13.5px}
.kv:last-child{border-bottom:none}
.kv .n{font-weight:680;font-variant-numeric:tabular-nums}
.kv .lbl{color:var(--ink-3)}
/* grid: колонка подписи авто-выравнивается по самой длинной → все бары стартуют ровно */
.hbars{display:grid;grid-template-columns:auto 1fr auto;gap:9px 11px;align-items:center;margin-top:4px}
.hbar{display:contents}
.hbar .hl{font-size:12.5px;color:var(--mut);font-variant-numeric:tabular-nums;white-space:nowrap}
.hbar .bar{width:100%}
.hbar .hn{text-align:right;font-weight:660;font-variant-numeric:tabular-nums;font-size:13px;white-space:nowrap}
/* table */
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:right;font-weight:600;color:var(--mut-2);font-size:10.5px;text-transform:uppercase;
  letter-spacing:.04em;padding:0 0 9px;border-bottom:1px solid var(--line)}
.tbl th:first-child,.tbl td:first-child{text-align:left}
.tbl td{padding:9px 0;border-bottom:1px solid var(--soft);font-variant-numeric:tabular-nums;text-align:right}
.tbl tr:last-child td{border-bottom:none}
.tbl td b{font-weight:680}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:2px}
.chip{background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:5px 10px;
  font-size:12.5px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.chip b{color:var(--ink);font-weight:660}
/* wiring / status */
.status{display:flex;align-items:center;gap:9px;font-size:13px;margin-bottom:13px;
  padding:9px 12px;border-radius:10px;background:var(--soft)}
.status.ok{background:var(--ok-bg)}.status.err{background:var(--bad-bg)}
.status .sic{width:16px;height:16px;flex:none}
.status.ok .sic{color:var(--ok-d)}.status.err .sic{color:var(--bad)}
.warns{font-size:12px;color:var(--warn);margin:-4px 0 12px;padding-left:2px;display:flex;align-items:center;gap:6px}
.warns .wic{width:14px;height:14px;flex:none}
.wire{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--soft);font-size:13px}
.wire:last-child{border-bottom:none}
.sdot{width:8px;height:8px;border-radius:50%;flex:none}
.sdot.on{background:var(--ok)}.sdot.off{background:var(--line-2)}
.wname{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--ink-2)}
.wlbl{color:var(--mut-2);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wseen{margin-left:auto;font-weight:660;font-variant-numeric:tabular-nums;font-size:12.5px}
.wseen.off{color:var(--line-3);font-weight:500}
.empty{color:var(--mut-3);font-size:13px;text-align:center;padding:26px 0}
/* генерик-панели */
.segwrap{display:inline-flex;background:var(--track);border-radius:10px;padding:3px;gap:2px;margin-bottom:15px}
.seg{padding:6px 14px;border-radius:8px;font-size:12.5px;font-weight:600;color:var(--mut);
  text-decoration:none;line-height:1;transition:.12s;white-space:nowrap}
.seg:hover{color:var(--ink)}
.seg.on{background:var(--card);color:var(--ink);box-shadow:0 1px 2px var(--sh-2)}
.banner{font-size:13px;line-height:1.5;padding:11px 14px;border-radius:11px;margin-bottom:14px}
.banner.info{background:var(--info-bg);color:var(--info-d)}
.banner.ok{background:var(--ok-bg);color:var(--ok-d)}
.banner.warn{background:var(--warn-bg);color:var(--warn-d)}
.banner.bad{background:var(--bad-bg);color:var(--bad-d)}
.banner b{font-weight:680}
.tiles{display:flex;flex-wrap:wrap;gap:11px}
.tile{flex:1;min-width:104px;background:var(--card-2);border:1px solid var(--track);border-radius:11px;
  padding:12px 13px;text-align:center}
.tile .tv{font-size:20px;font-weight:680;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile .tk{font-size:11.5px;color:var(--mut-2);margin-top:3px}
.lead{display:inline-block;font-size:10px;font-weight:700;color:var(--ok-d);background:var(--ok-bg-2);
  border-radius:5px;padding:1px 5px;margin-left:5px;vertical-align:middle;letter-spacing:.02em}
.sig{color:var(--ok-d);font-weight:680}
"""

# минималистичные line-иконки (SVG, без emoji — по правилу проекта)
_ICONS = {
    "funnel": '<path d="M2.5 4h11M4.5 8h7M6.5 12h3"/>',
    "money": '<path d="M6 3v10M6 3h3.2a2.4 2.4 0 0 1 0 4.8H5M5 9.2h4.5"/>',
    "clock": '<circle cx="8" cy="8" r="5.5"/><path d="M8 5v3.2l2.2 1.3"/>',
    "target": '<circle cx="8" cy="8" r="5.5"/><circle cx="8" cy="8" r="2.2"/>',
    "repeat": '<path d="M13 6.5A5 5 0 0 0 3.5 6M3 9.5A5 5 0 0 0 12.5 10"/><path d="M13 3.5V6.5H10M3 12.5V9.5H6"/>',
    "form": '<rect x="3" y="2.5" width="10" height="11" rx="1.8"/><path d="M5.8 6h4.4M5.8 8.6h4.4M5.8 11.2h2.6"/>',
    "bars": '<path d="M3 13V8.5M8 13V3.5M13 13V6.5"/>',
    "plug": '<path d="M6 2v2.5M10 2v2.5M4 5h8v2.5a4 4 0 0 1-8 0V5ZM8 11.5V14"/>',
    "spark": '<path d="M8 2.5l1.6 3.4 3.6.4-2.7 2.4.8 3.6L8 10.9l-3.3 1.8.8-3.6L2.8 6.7l3.6-.4z"/>',
    "refresh": '<path d="M13 8a5 5 0 1 1-1.5-3.5M13 2.5V5h-2.5"/>',
    "code": '<path d="M6 5 2.5 8 6 11M10 5l3.5 3-3.5 3"/>',
    "warn": '<path d="M8 2.5 14 13H2L8 2.5ZM8 6.5v3.2M8 11.4v.1"/>',
    "sun": '<circle cx="8" cy="8" r="3.1"/><path d="M8 1.4v1.6M8 13v1.6M1.4 8h1.6M13 8h1.6M3.3 3.3l1.2 1.2M11.5 11.5l1.2 1.2M12.7 3.3l-1.2 1.2M4.5 11.5l-1.2 1.2"/>',
    "moon": '<path d="M13.4 9.6A5.8 5.8 0 0 1 6.4 2.6a5.9 5.9 0 1 0 7 7z"/>',
}


def _ic(name: str, cls: str = "ic") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
            f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{_ICONS.get(name, "")}</svg>')


def _n(x) -> str:
    """Разряды пробелом: 12345 → 12 345."""
    try:
        return f"{int(round(float(x))):,}".replace(",", " ")
    except Exception:  # noqa: BLE001
        return str(x)


def _drop_pill(v: float | None) -> str:
    """Пилюля падения между шагами."""
    if v is None or v > 1 or v < 0:
        return ""
    c = "r" if v >= 0.4 else "y" if v >= 0.2 else "g"
    return f'<span class="pill {c}">−{v*100:.0f}%</span>'


# ---------- сводка сверху ----------

def _kpis(d: dict) -> str:
    tiles = []
    visits = d.get("visits", 0)
    tiles.append(("Визиты", _n(visits), "уники по сквозному id", ""))

    steps = d.get("steps") or []
    if len(steps) > 1:
        last = steps[-1]
        pct = last["pct_base"] * 100
        tiles.append(("Конверсия", f'{pct:.1f}<small>%</small>',
                      f'{_n(last["count"])} → {last["label"].lower()}', ""))

    ltv = d.get("ltv")
    money = d.get("money") or {}
    orders = None
    if ltv:
        orders = ltv["orders"]
    elif money.get("rows"):
        orders = money["rows"][-1]["count"]
    if orders is not None:
        sub = f'покупателей {_n(ltv["buyers"])}' if ltv else "оплат всего"
        tiles.append(("Оплаты", _n(orders), sub, ""))

    net = d.get("net")
    if net:
        gross = sum(r["gross"] for r in net)
        netsum = sum(r["net"] for r in net)
        cb = sum(r["cb_rate"] * r["gross"] for r in net)
        tiles.append(("NET-выручка", f'{_n(netsum)}<small> ₽</small>',
                      f'gross {_n(gross)} ₽', "good" if netsum > 0 else "bad"))
        cbrate = (cb / gross) if gross else 0
        tone = "bad" if cbrate > 0.01 else "good" if cbrate == 0 else ""
        tiles.append(("Chargeback", f'{cbrate*100:.1f}<small>%</small>',
                      "порог блокировки 1%", tone))
    elif ltv and ltv.get("aov"):
        tiles.append(("Средний чек", f'{_n(ltv["aov"])}<small> ₽</small>',
                      f'повторных {_n(ltv["repeat"])}', ""))

    # проектные доп-плитки (баланс поставщика, вовлечённость и т.п.)
    for k in d.get("extra_kpis") or []:
        tiles.append((k.get("label", ""), k.get("value", ""), k.get("sub", ""), k.get("tone", "")))

    cells = "".join(
        f'<div class="kpi {tone}"><div class="l">{lbl}</div>'
        f'<div class="v">{val}</div><div class="s">{sub}</div></div>'
        for lbl, val, sub, tone in tiles)
    return f'<div class="kpis">{cells}</div>'


# ---------- генерик-панели (проектные секции: A/B, тарифы, ops) ----------

def _render_panel(p: dict) -> str:
    """Рендер произвольной проектной панели по структуре (модуль-агностично).
    Ключи (в порядке вывода): controls, banner{text,tone}, tiles[{v,k}],
    table{head,rows}, kv[[k,v]], chips[[k,v]], bars[[label,count]], hint."""
    parts = []
    ctrls = p.get("controls")
    if ctrls:
        segs = "".join(
            f'<a class="seg{" on" if c.get("active") else ""}" href="{c.get("href","#")}">{c.get("label","")}</a>'
            for c in ctrls)
        parts.append(f'<div class="segwrap">{segs}</div>')
    ban = p.get("banner")
    if ban and ban.get("text"):
        parts.append(f'<div class="banner {ban.get("tone","info")}">{ban["text"]}</div>')
    tiles = p.get("tiles")
    if tiles:
        parts.append('<div class="tiles">' + "".join(
            f'<div class="tile"><div class="tv">{t.get("v","")}</div>'
            f'<div class="tk">{t.get("k","")}</div></div>' for t in tiles) + '</div>')
    tbl = p.get("table")
    if tbl and tbl.get("rows"):
        # Ячейки экранируются ПО УМОЛЧАНИЮ (защита от XSS через untrusted-данные вроде
        # utm_campaign). Доверенный HTML передаётся явно как {"html": "<b>…</b>"}.
        def _cell(c):
            if isinstance(c, dict) and "html" in c:
                return f'<td>{c["html"]}</td>'
            return f'<td>{_esc(str(c))}</td>'
        head = "".join(f'<th>{_esc(str(h))}</th>' for h in tbl.get("head", []))
        body = "".join('<tr>' + "".join(_cell(c) for c in row) + '</tr>'
                       for row in tbl["rows"])
        parts.append(f'<table class="tbl"><tr>{head}</tr>{body}</table>')
    kv = p.get("kv")
    if kv:
        parts.append("".join(
            f'<div class="kv"><span class="lbl">{k}</span><span class="n">{v}</span></div>'
            for k, v in kv))
    chips = p.get("chips")
    if chips:
        parts.append('<div class="chips">' + "".join(
            f'<span class="chip">{k} <b>{v}</b></span>' for k, v in chips) + '</div>')
    bars = p.get("bars")
    if bars:
        top = max((c for _, c in bars), default=0) or 1
        parts.append('<div class="hbars">' + "".join(
            f'<div class="hbar"><span class="hl">{lbl}</span>'
            f'<div class="bar"><i style="width:{max(1.5,c/top*100):.1f}%"></i></div>'
            f'<span class="hn">{_n(c)}</span></div>' for lbl, c in bars) + '</div>')
    if p.get("hint"):
        parts.append(f'<div class="hint">{p["hint"]}</div>')
    icon = _ic(p.get("icon", "spark"))
    tag = f'<span class="tag">{p["tag"]}</span>' if p.get("tag") else ""
    return (f'<div class="card full"><h2>{icon}{p.get("title","")}{tag}</h2>'
            + "".join(parts) + '</div>')


def _panels_block(d: dict) -> str:
    panels = [p for p in (d.get("panels") or []) if p]
    if not panels:
        return ""
    # display:contents — обёртка не создаёт бокс, карточки остаются прямыми детьми сетки;
    # id нужен для точечной подмены при переключении src (без перезагрузки страницы).
    return ('<div id="an-panels" style="grid-column:1/-1;display:contents;transition:opacity .15s">'
            + "".join(_render_panel(p) for p in panels) + '</div>')


# ---------- карточки ----------

def _funnel_block(d: dict) -> str:
    steps = d["steps"]
    if not steps:
        return ""
    top = max((s["count"] for s in steps), default=0) or 1
    rows = ""
    for s in steps:
        w = max(1.5, s["count"] / top * 100)
        obj = ""
        if s.get("obj_here") and d.get("obj") and sum(d["obj"].values()):
            o = d["obj"]
            obj = (f'<div class="fobj">адрес <b>{_n(o.get("address",0))}</b> · '
                   f'кадастр <b>{_n(o.get("cadastre",0))}</b></div>')
        rows += (
            f'<div class="frow"><div class="fhead">'
            f'<span class="flabel">{s["label"]}</span>'
            f'<span class="fval">{_n(s["count"])}</span>'
            f'<span class="fpct">{s["pct_base"]*100:.0f}% визитов</span>'
            f'{_drop_pill(s["pct_prev"] is not None and (1 - s["pct_prev"]) or None)}'
            f'</div><div class="bar"><i style="width:{w:.1f}%"></i></div>{obj}</div>')
    return ('<div class="card full"><h2>' + _ic("funnel") + 'Воронка</h2>' + rows + '</div>')


def _abandon_block(d: dict) -> str:
    ab = d.get("abandon")
    if not ab:
        # метрика настроена, но событий ухода на загрузке ещё нет — показываем
        # честное пустое состояние (иначе кажется, что метрики нет вовсе)
        if d.get("abandon_tracked"):
            return ('<div class="card"><h2>' + _ic("clock") + 'Отвал на загрузке'
                    '<span class="tag">через сколько уходят</span></h2>'
                    '<div class="empty">Пока никто не уходил во время загрузки результата '
                    '(или в этом окне ещё нет таких событий).<br>Трекинг включён: секунда ухода '
                    'считается по heartbeat (пинг «я тут» раз в 5с) + финальному сигналу при '
                    'закрытии/переключении вкладки. Точность ±интервал.</div></div>')
        return ""
    mx = max((c for _, c in ab["dist"]), default=0) or 1
    bars = "".join(
        f'<div class="hbar"><span class="hl">{lbl}</span>'
        f'<div class="bar amber"><i style="width:{c/mx*100:.0f}%"></i></div>'
        f'<span class="hn">{c}</span></div>' for lbl, c in ab["dist"])
    return ('<div class="card"><h2>' + _ic("clock") + 'Отвал на загрузке'
            f'<span class="tag">ушло {ab["n"]}</span></h2>'
            f'<div class="chips" style="margin-bottom:12px">'
            f'<span class="chip">медиана <b>{ab["median"]}с</b></span>'
            f'<span class="chip">среднее <b>{ab["avg"]}с</b></span>'
            f'<span class="chip">пик <b>{ab["max_bucket"]}</b></span></div>'
            f'<div class="hbars">{bars}</div>'
            '<div class="hint">Большинство уходит раньше таймера → сократить загрузку до медианы.</div></div>')


def _money_block(d: dict) -> str:
    m = d.get("money")
    if not m or not m.get("rows"):
        return ""
    top = max((r["count"] for r in m["rows"]), default=0) or 1
    def _mrow(r):
        pill = _drop_pill(r["drop"])
        return (f'<div class="hbar"><span class="hl" style="color:#4a5568">{r["label"]}</span>'
                f'<div class="bar"><i style="width:{max(1.5,r["count"]/top*100):.1f}%"></i></div>'
                f'<span class="hn">{_n(r["count"])}{("&nbsp;&nbsp;" + pill) if pill else ""}</span></div>')
    rows = "".join(_mrow(r) for r in m["rows"])
    extra = ""
    if m["bump_rate"] is not None:
        extra += (f'<div class="kv"><span class="lbl">Апселл берут</span>'
                  f'<span class="n">{_n(m["bump_yes"])}/{_n(m["bump_total"])} · {m["bump_rate"]*100:.0f}%</span></div>')
    if m.get("method_mix"):
        chips = "".join(f'<span class="chip">{k} <b>{_n(v)}</b></span>'
                        for k, v in sorted(m["method_mix"].items(), key=lambda kv: -kv[1]))
        extra += f'<div class="chips" style="margin-top:11px">{chips}</div>'
    return ('<div class="card"><h2>' + _ic("money") + 'Денежная петля</h2>'
            f'<div class="hbars" style="margin-bottom:6px">{rows}</div>{extra}'
            '<div class="hint">Отвал «шлюз → оплата» — проблема страницы оплаты. СБП даёт меньше чарджбэков.</div></div>')


def _net_block(d: dict) -> str:
    net = d.get("net")
    if not net:
        return ""
    rows = "".join(
        f'<tr><td><b>{r["variant"]}</b> · {_n(r["price"])} ₽</td><td>{_n(r["orders"])}</td>'
        f'<td>{_n(r["gross"])}</td><td><b>{_n(r["net"])}</b></td><td>{_n(r["net_per_order"])}</td>'
        f'<td><span class="pill {"r" if r["cb_rate"]>0.01 else "g"}">{r["cb_rate"]*100:.1f}%</span></td></tr>'
        for r in net)
    return ('<div class="card full"><h2>' + _ic("bars") + 'NET-маржа по варианту цены'
            '<span class="tag">не gross</span></h2>'
            '<table class="tbl"><tr><th>Вариант</th><th>Оплат</th><th>Gross ₽</th>'
            '<th>NET ₽</th><th>NET/заказ</th><th>CB-rate</th></tr>' + rows + '</table>'
            '<div class="hint">Победитель A/B — по NET на заказ, не по кассе. '
            'CB-rate &gt; 1% (красным) грозит блокировкой мерчанта — вариант убить.</div></div>')


def _lift_block(d: dict) -> str:
    lf = d.get("lift")
    if not lf or not (lf["with"]["n"] or lf["without"]["n"]):
        return ""
    w, wo = lf["with"], lf["without"]
    def row(name, x):
        return (f'<div class="kv"><span class="lbl">{name}</span>'
                f'<span class="n">{_n(x["n"])} → {_n(x["conv"])} <span class="fpct">({x["rate"]*100:.0f}%)</span></span></div>')
    return ('<div class="card"><h2>' + _ic("target") + 'Объект найден → конверсия</h2>'
            + row("С объектом", w) + row("Без объекта", wo)
            + '<div class="hint">«С объектом» конвертит лучше → бесплатное превью окупается.</div></div>')


def _ltv_block(d: dict) -> str:
    lt = d.get("ltv")
    if not lt:
        return ""
    rows = (f'<div class="kv"><span class="lbl">Покупателей / оплат</span>'
            f'<span class="n">{_n(lt["buyers"])} / {_n(lt["orders"])}</span></div>')
    rr = "" if lt["repeat_rate"] is None else f' · {lt["repeat_rate"]*100:.0f}%'
    rows += (f'<div class="kv"><span class="lbl">Повторные</span>'
             f'<span class="n">{_n(lt["repeat"])}{rr}</span></div>')
    if lt.get("aov"):
        rows += (f'<div class="kv"><span class="lbl">Средний чек</span>'
                 f'<span class="n">{_n(lt["aov"])} ₽</span></div>')
    return ('<div class="card"><h2>' + _ic("repeat") + 'Повторные и средний чек</h2>' + rows +
            '<div class="hint">Мало повторных → письмо «проверь ещё» = почти бесплатный доп-доход.</div></div>')


def _friction_block(d: dict) -> str:
    fr = d.get("friction")
    if not fr:
        return ""
    top = max((v for _, v in fr), default=0) or 1
    rows = "".join(
        f'<div class="hbar"><span class="hl">{k}</span>'
        f'<div class="bar amber"><i style="width:{v/top*100:.0f}%"></i></div>'
        f'<span class="hn">{_n(v)}</span></div>' for k, v in fr)
    return ('<div class="card"><h2>' + _ic("form") + 'Трение формы<span class="tag">ушли не запустив</span></h2>'
            f'<div class="hbars">{rows}</div>'
            '<div class="hint">На каком поле бросают. «none» = уходят сразу, не тронув форму.</div></div>')


def _signals_block(d: dict) -> str:
    """Прочие собираемые сигналы (клики CTA, показ апселла, просмотр демо и т.п.) —
    счётчики уников, чтобы ничто собираемое не оставалось невидимым."""
    sig = [s for s in (d.get("signals") or []) if s]
    if not sig:
        return ""
    top = max((s["count"] for s in sig), default=0) or 1
    rows = "".join(
        f'<div class="hbar"><span class="hl" style="color:#4a5568">{s["label"]}</span>'
        f'<div class="bar"><i style="width:{max(1.5,s["count"]/top*100):.1f}%"></i></div>'
        f'<span class="hn">{_n(s["count"])}</span></div>' for s in sig)
    return ('<div class="card"><h2>' + _ic("spark") + 'Прочие сигналы<span class="tag">уники</span></h2>'
            f'<div class="hbars">{rows}</div>'
            '<div class="hint">Доп-события интереса (демо, CTA, показ апселла) — вне основной воронки.</div></div>')


def _wiring_block(d: dict) -> str:
    """Состояние подключения (self-check) прямо на странице."""
    sc = d.get("selfcheck")
    if not sc:
        return ""
    cfg_v = sc.get("config", {})
    ok = sc.get("ok")
    st = sc.get("storage", {})
    if cfg_v.get("errors"):
        head = (f'<div class="status err">{_ic("plug","sic")}<span>Ошибки конфига: '
                f'{"; ".join(cfg_v.get("errors",[]))}</span></div>')
    else:
        head = (f'<div class="status ok">{_ic("plug","sic")}<span>Конфиг валиден · '
                f'{_n(st.get("events",0))} событий · {_n(st.get("sessions",0))} сессий</span></div>')
    # Потерянные события — отдельной красной строкой, а не сноской: имя вне конфига
    # отбрасывается молча (ручка отвечает 200 OK), и заметить пропажу иначе нечем.
    rej = sc.get("rejected") or []
    if rej:
        lst = ", ".join(f'<b>{_esc(r["event"])}</b> ×{_n(r["count"])}' for r in rej[:8])
        head += (f'<div class="status err">{_ic("warn","sic")}<span>Фронт шлёт события, '
                 f'которых нет в конфиге, и они теряются: {lst}. Добавь их в '
                 f'extra_events/signal_labels — или убери вызов на фронте.</span></div>')
    warns = ""
    if cfg_v.get("warnings"):
        warns = ('<div class="warns">' + _ic("warn", "wic") + " · ".join(cfg_v["warnings"]) + '</div>')
    wire = "".join(
        f'<div class="wire"><span class="sdot {"on" if w["seen"] else "off"}"></span>'
        f'<span class="wname">{w["event"]}</span>'
        f'<span class="wlbl">{w["label"]}</span>'
        f'<span class="wseen {"" if w["seen"] else "off"}">{_n(w["seen"])}</span></div>'
        for w in sc.get("wiring", []))
    return ('<div class="card full"><h2>' + _ic("plug") + 'Состояние подключения'
            '<span class="tag">событие → приходит?</span></h2>'
            + head + warns + wire +
            '<div class="hint">Серая точка и 0 = фронт в этой точке ещё не отправляет событие.</div></div>')


def _campaign_matrix_block(d: dict) -> str:
    """Матрица кампаний: строка на кампанию × вехи воронки + деньги. Клик по строке →
    фильтрует весь дашборд на эту кампанию (drill-down)."""
    m = d.get("campaign_matrix")
    if not m or not m.get("rows"):
        return ""
    token = d.get("campaign_token", "")
    sel = m.get("selected", "all")
    funnel = m.get("funnel", [])
    if not funnel:
        return ""
    last_name, last_label = funnel[-1]
    rows = m["rows"]
    tot_v = sum(r["visits"] for r in rows)
    tot_last = sum(r["steps"].get(last_name, 0) for r in rows)
    tot_paid = sum(r["paid"] for r in rows)
    tot_rev = sum(r["revenue"] for r in rows)

    def _row(camp, visits, last, paid, rev, is_total=False):
        cvr = f'{paid/visits*100:.2f}%' if visits else "—"
        aov = f'{_n(round(rev/paid))} ₽' if paid else "—"
        act = (not is_total and camp == sel)
        style = ' style="background:#eef2fb"' if act else ''
        if is_total:
            name = '<b>Итого</b>'
        else:
            # camp = utm_campaign (управляется атакующим через ссылку) → экранируем текст
            # и url-энкодим для href, иначе stored-XSS в админ-дашборде (кража токена).
            href = f'?token={_q(str(token))}&campaign={_q(str(camp))}'
            name = f'<a href="{href}" style="font-weight:640;color:#2c3e66">{_esc(str(camp))}</a>'
        return (f'<tr{style}><td>{name}</td><td>{_n(visits)}</td><td>{_n(last)}</td>'
                f'<td><b>{_n(paid)}</b></td><td><b>{_n(rev)} ₽</b></td>'
                f'<td>{cvr}</td><td>{aov}</td></tr>')
    body = "".join(_row(r["camp"], r["visits"], r["steps"].get(last_name, 0),
                        r["paid"], r["revenue"]) for r in rows)
    body += _row("", tot_v, tot_last, tot_paid, tot_rev, is_total=True)
    tag = (f'фильтр: {_esc(str(sel))}' if sel != "all" else 'клик по кампании → весь дашборд по ней')
    reset = (f'<a class="btn" href="?token={_q(str(token))}&campaign=all" style="margin-left:auto">'
             + _ic("refresh") + 'все кампании</a>') if sel != "all" else ""
    return ('<div class="card full"><h2>' + _ic("bars") + 'Матрица кампаний'
            f'<span class="tag">{tag}</span>{reset}</h2>'
            '<div style="overflow-x:auto"><table class="tbl">'
            f'<tr><th>Кампания</th><th>Визиты</th><th>{last_label}</th><th>Оплатили</th>'
            '<th>Выручка</th><th>CVR визит→опл.</th><th>Ср.чек</th></tr>'
            + body + '</table></div>'
            '<div class="hint">Строка = кампания (utm), «Визиты→Оплатили→Выручка» бок о бок. '
            'Клик по кампании фильтрует всю страницу (воронка/деньги/отвал) на неё; «все кампании» — сброс.</div></div>')


def render_dashboard(cfg, d: dict[str, Any]) -> str:
    since = (d.get("since") or "").replace("T", " ")[:16]
    since_txt = f'с {since}' if since else 'за всё время'
    # плашка активного фильтра кампании (drill-down)
    camp_sel = d.get("campaign_selected", "all")
    filt = ('' if camp_sel in ("all", None) else
            f'<div class="banner bad" style="grid-column:1/-1;margin-bottom:14px">'
            f'Дашборд отфильтрован на кампанию <b>{_esc(str(camp_sel))}</b> — воронка, деньги и отвал показаны '
            f'только по ней. <a href="?token={_q(str(d.get("campaign_token","")))}&campaign=all" '
            f'style="font-weight:680">← показать все кампании</a></div>')
    d["_filter_banner"] = filt
    empty = ('<div class="card full"><div class="empty">Событий пока нет — '
             'проверь, что фронт шлёт <code>Analytics.goal(...)</code>. '
             'Состояние подключения ниже.</div></div>' if not d.get("visits") else "")
    body = (
        d.get("_filter_banner", "") +
        _campaign_matrix_block(d) +
        _panels_block(d) +
        _funnel_block(d) + _money_block(d) + _net_block(d) +
        _abandon_block(d) + _lift_block(d) + _ltv_block(d) +
        _friction_block(d) + _signals_block(d) + _wiring_block(d))
    return (
        '<!doctype html><html lang=ru><head><meta charset=utf-8>'
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f'<title>{cfg.title}</title><style>{_CSS}</style>' + _THEME_JS + '</head><body>'
        '<div class="top"><div class="top-in">'
        f'<div class="brand"><span class="mark">{_ic("spark")}</span><h1>{cfg.title}</h1></div>'
        '<div class="meta">'
        f'<span class="live"><span class="dot"></span><b>{_n(d.get("visits",0))}</b>&nbsp;визитов</span>'
        f'<span>{since_txt}</span>'
        '<button class="btn" id="themeBtn" type="button" onclick="csTheme()" '
        'title="Светлая / тёмная тема" aria-label="Переключить тему">'
        + _ic("sun", "ic-sun") + _ic("moon", "ic-moon") + '<span id="themeLbl">Тёмная</span></button>'
        '<a class="btn" href="#" onclick="location.reload();return false">' + _ic("refresh") + 'Обновить</a>'
        '<a class="btn" href="#" onclick="location.href=\'/admin/stats.json\'+location.search;return false">'
        + _ic("code") + 'JSON</a>'
        '</div></div></div>'
        f'<div class="wrap">{_kpis(d)}{empty}<div class="grid">{body}</div></div>'
        + _SEG_JS + '</body></html>')


# Переключение src без перезагрузки: клик по сегменту подгружает страницу через fetch,
# подменяет только #an-panels и правит URL. Прогрессивное улучшение — при любой ошибке
# просто переходит по ссылке (обычная навигация).
_THEME_JS = """<script>
/* Тема дашборда. Ставится ДО первой отрисовки — если делать это после, тёмная
   тема на секунду мигнёт белым. Выбор запоминается; пока его нет, слушаемся
   системной настройки, а не навязываем светлую. */
(function(){
  var K='cs_admin_theme';
  function apply(t){
    document.documentElement.setAttribute('data-theme', t);
    var l=document.getElementById('themeLbl');
    if(l) l.textContent = (t==='dark' ? 'Светлая' : 'Тёмная');
  }
  var saved=null;
  try{ saved=localStorage.getItem(K); }catch(e){}
  var sys = (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark':'light';
  apply(saved || sys);
  window.csTheme=function(){
    var now=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
    try{ localStorage.setItem(K, now); }catch(e){}
    apply(now);
  };
  // системную тему слушаем, только пока пользователь сам ничего не выбрал
  if(!saved && window.matchMedia){
    try{ matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(e){
      var s=null; try{ s=localStorage.getItem(K); }catch(_){}
      if(!s) apply(e.matches?'dark':'light');
    }); }catch(e){}
  }
  document.addEventListener('DOMContentLoaded', function(){
    apply(document.documentElement.getAttribute('data-theme')||'light');
  });
})();
</script>"""

_SEG_JS = """<script>
document.addEventListener('click',function(e){
  var a=e.target.closest&&e.target.closest('.seg');
  if(!a)return;var url=a.getAttribute('href');if(!url||url.indexOf('src=')<0)return;
  var box=document.getElementById('an-panels');if(!box){return;}
  e.preventDefault();
  Array.prototype.forEach.call(box.querySelectorAll('.card'),function(c){c.style.opacity=.45;});
  fetch(url,{credentials:'same-origin'}).then(function(r){return r.text();}).then(function(html){
    var doc=new DOMParser().parseFromString(html,'text/html');
    var fresh=doc.getElementById('an-panels');
    if(!fresh)throw 0;
    box.innerHTML=fresh.innerHTML;
    try{history.replaceState(null,'',url);}catch(_){}
  }).catch(function(){location.href=url;});
});
</script>"""
