"""
PDF-отчёт «ЧистаяСделка Авто» через reportlab.

Файл повторяет ЭКРАН отчёта (static/report.html) — тот же порядок блоков, те же
формулировки, тот же визуальный язык: белый лист, волосяные линии, подписи
источников капителью, единственный цвет — у риска. Раньше PDF был свёрстан в
«корпоративном» ключе (тёмная шапка, синие заголовки с подчёркиванием, таблицы
с чёрными шапками), и человек, заплативший за экран, получал на почту документ,
который выглядел и читался иначе.

Тексты плиток берутся из modules/report_copy — единого источника, общего со
страницей. Кириллица — DejaVuSans.ttf, лежит в static/.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import report_copy
from .verdict import Check, Verdict, RISK_HIGH, RISK_LOW, RISK_MEDIUM

_HERE = Path(__file__).resolve().parent
_FONT_PATH = _HERE.parent / "static" / "DejaVuSans.ttf"
_FONT_REGISTERED = False


def _register_font() -> str:
    global _FONT_REGISTERED
    if _FONT_REGISTERED:
        return "DejaVu"
    if _FONT_PATH.exists():
        pdfmetrics.registerFont(TTFont("DejaVu", str(_FONT_PATH)))
        _FONT_REGISTERED = True
        return "DejaVu"
    # fallback: reportlab default (кириллица не отобразится, но не упадём)
    return "Helvetica"


RISK_COLOR = {
    RISK_LOW: colors.HexColor("#16a34a"),
    RISK_MEDIUM: colors.HexColor("#d97706"),
    RISK_HIGH: colors.HexColor("#dc2626"),
}

STATUS_ICON = {"not_found": "✓", "found": "⚠", "not_checked": "?", "locked": "·"}

# Подписи относятся к ПРЕДМЕТУ ПОИСКА (обременения, аресты), а не к объекту:
# «не найдено» рядом с «объект найден» читается как противоречие — на этом мы
# уже получили требование возврата. Совпадают с экраном (report_copy).
STATUS_LABEL = {
    "not_found": "чисто",
    "found": "обнаружено",
    "not_checked": "нет данных",
    "locked": "не входит в тариф",
}

# палитра экрана
INK = colors.HexColor("#0a0a0a")
MUTED = colors.HexColor("#6b7280")
FAINT = colors.HexColor("#9aa4b2")
LINE = colors.HexColor("#e5e7eb")
HAIR = colors.HexColor("#f1f3f6")
SOFTBG = colors.HexColor("#f8fafc")
OK = colors.HexColor("#16a34a")
WARN = colors.HexColor("#d97706")
BAD = colors.HexColor("#dc2626")
ACCENT = colors.HexColor("#2563EB")

USABLE = 174 * mm


def _esc(s: Any) -> str:
    return (str(s or "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _ru_date(iso: str) -> str:
    """2026-08-08 → 08.08.2026. Чужие форматы отдаём как есть."""
    s = str(iso or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return f"{s[8:10]}.{s[5:7]}.{s[0:4]}"
    return s


def render_report(
    out_path: str,
    *,
    report_id: str,
    seller_name: str = "",
    seller_dob: str = "",
    object_ref: str = "",
    email: str = "",
    checks: list[Check],
    verdict: Verdict,
    addon: dict | None = None,
    deal_kit: dict | None = None,
    passport: dict | None = None,
    owners: list | None = None,
    schemes: list | None = None,
    car: dict | None = None,
    facts: list | None = None,
    vin_check: dict | None = None,
) -> str:
    font = _register_font()
    styles = getSampleStyleSheet()
    passport = passport or {}
    owners = owners or []
    car = car or {}
    facts = facts or []
    vin_check = vin_check or {}
    rc = RISK_COLOR.get(verdict.risk, INK)

    # ── стили под экран ──────────────────────────────────────────────────────
    def st(name, size, leading, color=INK, **kw):
        return ParagraphStyle(name, parent=styles["BodyText"], fontName=font,
                              fontSize=size, leading=leading, textColor=color,
                              spaceBefore=0, spaceAfter=0, **kw)

    s_h1 = st("h1", 21, 25)
    s_h2 = st("h2", 14.5, 18)
    s_meta = st("meta", 9, 12, MUTED)
    s_label = st("label", 6.5, 9, FAINT)          # капитель над значением
    s_value = st("value", 10.5, 13)
    s_body = st("body", 9.5, 14, colors.HexColor("#374151"))
    s_muted = st("muted", 8.5, 12, MUTED)
    s_faint = st("faint", 7.5, 10, FAINT)
    s_sub = st("sub", 8.5, 12, FAINT)
    s_tile_t = st("tt", 9.5, 12.5)
    s_tile_m = st("tm", 8, 11, colors.HexColor("#4b5563"))
    s_row_n = st("rn", 9.5, 12.5)
    s_note = st("note", 8.5, 12, WARN)
    s_verd = st("verd", 15, 19)

    def val(text, color=INK, size=10.5, bold=False):
        return Paragraph(f"<b>{_esc(text)}</b>" if bold else _esc(text),
                         st("v", size, size + 3, color))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(FAINT)
        canvas.drawString(18 * mm, 12 * mm, "ЧистаяСделка Авто · avto.chistasdelka.ru")
        canvas.drawRightString(192 * mm, 12 * mm, f"Отчёт {report_id} · стр. {doc_.page}")
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 15 * mm, 192 * mm, 15 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=20 * mm,
        title="ЧистаяСделка Авто — отчёт по автомобилю",
    )
    story: list = []

    def section(title: str, sub: str = "") -> None:
        story.append(Spacer(1, 18))
        story.append(Paragraph(f"<b>{_esc(title)}</b>", s_h2))
        if sub:
            story.append(Spacer(1, 3))
            story.append(Paragraph(_esc(sub), s_sub))
        story.append(Spacer(1, 9))

    # ── шапка: бренд + предупреждение об архивности ─────────────────────────
    story.append(Paragraph(
        "<b>Чистая</b><font color='#2563EB'><b>Сделка</b></font> <b>Авто</b>",
        st("brand", 12, 15)))
    story.append(Spacer(1, 12))

    tiles = [report_copy.tile(c.key, c.status, c.detail) for c in checks]
    oldest = sorted({t["asof"] for t in tiles if t["asof"] and t["asof_old"]})
    if oldest:
        story.append(Paragraph(
            f"Часть сведений архивные — самые старые от {oldest[0]}. "
            f"Даты указаны у каждой проверки.", s_note))
        story.append(Spacer(1, 10))

    # ── заголовок машины ────────────────────────────────────────────────────
    title = " ".join(x for x in (car.get("marka"), car.get("model")) if x) or \
        passport.get("model") or object_ref or "Автомобиль"
    story.append(Paragraph(f"<b>{_esc(title)}</b>", s_h1))
    story.append(Spacer(1, 6))

    meta_bits = [f"<font color='#9aa4b2'>VIN</font> <b>{_esc(object_ref)}</b>"]
    if car.get("body"):
        meta_bits.append(f"<font color='#9aa4b2'>кузов</font> {_esc(car['body'])}")
    meta_bits.append(f"<font color='#9aa4b2'>отчёт от</font> "
                     f"{dt.datetime.now().strftime('%d.%m.%Y')}")
    story.append(Paragraph("&nbsp;&nbsp;·&nbsp;&nbsp;".join(meta_bits), s_meta))

    # ── паспорт автомобиля: полоса ячеек, как на экране ─────────────────────
    if passport:
        def cell(label: str, value: str):
            if not value:
                return ""
            return [Paragraph(label.upper(), s_label), Spacer(1, 3),
                    Paragraph(f"<b>{_esc(value)}</b>", s_value)]

        pwr = " · ".join(x for x in (
            f"{passport.get('power_hp')} л.с." if passport.get("power_hp") else "",
            f"{passport.get('power_kw')} кВт" if passport.get("power_kw") else "") if x)
        mass = passport.get("mass") or ""
        if mass and passport.get("mass_max"):
            mass = f"{mass} кг из {passport['mass_max']}"
        elif mass:
            mass = f"{mass} кг"

        r1 = [cell("Модель по ПТС", passport.get("model", "")),
              cell("Год выпуска", passport.get("year", "")),
              cell("Цвет", passport.get("color", "")),
              cell("Объём двигателя",
                   f"{passport['volume']} см³" if passport.get("volume") else ""),
              cell("Мощность", pwr)]
        r2 = [cell("Категория", passport.get("category", "")),
              cell("Экологический класс", passport.get("eco", "")),
              cell("Масса снаряж. / разреш.", mass), "", ""]
        rows = [r for r in (r1, r2) if any(r)]
        if rows:
            story.append(Spacer(1, 12))
            w = USABLE / 5
            t = Table(rows, colWidths=[w] * 5)
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BOX", (0, 0), (-1, -1), 0.6, LINE),
                # Вертикальные волоски между ячейками — ровно как на экране.
                ("LINEAFTER", (0, 0), (-2, -1), 0.6, LINE),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE) if len(rows) > 1 else
                ("LINEBELOW", (0, 0), (-1, 0), 0, colors.white),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]))
            story.append(t)
            story.append(Spacer(1, 6))
            story.append(Paragraph(
                "Паспорт автомобиля по регистрационным данным ГИБДД"
                + (f" на {_ru_date(passport.get('as_of', ''))}" if passport.get("as_of") else "")
                + ". Сверьте с ПТС продавца.", s_faint))

    # ── вердикт слева, сводка справа ────────────────────────────────────────
    left: list = [Paragraph("ВЫВОД ПО ПРОВЕРКЕ", s_label), Spacer(1, 8)]
    pill = Table([[Paragraph(f"<b>Риск {_esc(verdict.risk)}</b>", st("p", 9.5, 12, rc))]],
                 colWidths=[32 * mm])
    pill.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fffbeb")),
        ("ROUNDEDCORNERS", [7, 7, 7, 7]),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    left += [pill, Spacer(1, 10),
             Paragraph(f"<b>{_esc(verdict.headline)}</b>", s_verd), Spacer(1, 8)]
    for f in facts[:3]:
        if isinstance(f, (list, tuple)) and len(f) >= 2:
            left.append(Paragraph(
                f"<font color='#d97706'>•</font>&nbsp; <b>{_esc(f[0])}: {_esc(f[1])}</b>",
                st("fb", 9, 13)))
            left.append(Spacer(1, 4))
    # Тело заключения НЕ кладём в ячейку таблицы: одна строка таблицы неделима,
    # и вместе с длинным текстом блок перестаёт помещаться в остаток страницы —
    # целиком уезжает на следующую, оставляя первую наполовину пустой.
    # Поэтому в колонке остаётся шапка вердикта, а текст идёт ниже и свободно
    # переносится через страницы.

    right: list = [Paragraph("ГЛАВНОЕ ИЗ ОТЧЁТА", s_label), Spacer(1, 7)]
    srows = []
    for c, t in zip(checks, tiles):
        col = OK if c.status == "not_found" else (
            FAINT if c.status in ("not_checked", "locked") else (WARN if t["soft"] else BAD))
        srows.append([Paragraph(_esc(c.name), s_row_n),
                      Paragraph(f"<b>{_esc(t['word'])}</b>", st("sw", 8.5, 12, col))])
    if srows:
        tb = Table(srows, colWidths=[38 * mm, 24 * mm])
        tb.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, HAIR),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        right.append(tb)
    # «Коротко о машине» и контрольный символ VIN на экране стоят в той же
    # колонке. На листе они делают её выше остатка страницы, и неделимая строка
    # таблицы уносит на следующий лист ВЕСЬ блок — первая страница остаётся
    # наполовину пустой. Поэтому здесь они идут отдельной парой ниже.
    story.append(Spacer(1, 18))
    two = Table([[left, right]], colWidths=[USABLE * 0.60, USABLE * 0.40])
    two.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (1, 0), (1, 0), SOFTBG),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 16),
        ("LEFTPADDING", (1, 0), (1, 0), 14), ("RIGHTPADDING", (1, 0), (1, 0), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 14), ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("ROUNDEDCORNERS", [0, 12, 12, 0]),
    ]))
    story.append(two)
    if verdict.body:
        story.append(Spacer(1, 12))
        story.append(Paragraph(_esc(verdict.body), s_body))

    # ── коротко о машине + контрольный символ VIN ───────────────────────────
    fcol: list = []
    if facts:
        frows = [[Paragraph(_esc(f[0]), s_row_n),
                  Paragraph(f"<b>{_esc(f[1])}</b>", st("fv", 9, 12))]
                 for f in facts if isinstance(f, (list, tuple)) and len(f) >= 2]
        if frows:
            fcol = [Paragraph("КОРОТКО О МАШИНЕ", s_label), Spacer(1, 7)]
            tb = Table(frows, colWidths=[52 * mm, 26 * mm])
            tb.setStyle(TableStyle([
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, HAIR),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                # Зазор между названием и значением: без него длинная подпись
                # упиралась в цифру и читалась как «Владельцев по учёту2».
                ("RIGHTPADDING", (0, 0), (0, -1), 10),
                ("RIGHTPADDING", (1, 0), (1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]))
            fcol.append(tb)
    vcol: list = []
    if vin_check.get("why"):
        okv = vin_check.get("ok")
        word = "сошёлся" if okv else ("не сошёлся" if okv is False else "не проверяли")
        col = OK if okv else (BAD if okv is False else FAINT)
        vcol = [Paragraph("КОНТРОЛЬНЫЙ СИМВОЛ VIN", s_label), Spacer(1, 6),
                Paragraph(f"<b>{word}</b>", st("vw", 9.5, 12, col)), Spacer(1, 4),
                Paragraph(_esc(vin_check["why"]), s_muted)]
    if fcol or vcol:
        story.append(Spacer(1, 18))
        pair = Table([[fcol or "", vcol or ""]],
                     colWidths=[USABLE * 0.52, USABLE * 0.48])
        pair.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 18),
            ("LEFTPADDING", (1, 0), (1, 0), 0), ("RIGHTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(pair)

    # ── плитки проверок ─────────────────────────────────────────────────────
    section("Что установлено по реестрам", "У каждой проверки — своя дата актуальности.")
    cells = []
    for c, t in zip(checks, tiles):
        dot = OK if c.status == "not_found" else (
            FAINT if c.status in ("not_checked", "locked") else (WARN if t["soft"] else BAD))
        # Ширину шапки плитки считаем от РЕАЛЬНОЙ ширины ячейки за вычетом полей.
        # Зашитые 42+6 мм были шире колонки, и точка выдавливалась вплотную к
        # заголовку — «Владельцев по учёту: 2●».
        _tw = USABLE / 3 - 22
        inner: list = [Table([[Paragraph(f"<b>{_esc(t['title'])}</b>", s_tile_t),
                               Paragraph("●", st("d", 7, 9, dot))]],
                             colWidths=[_tw - 14, 14])]
        inner[0].setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        if t["means"]:
            inner += [Spacer(1, 6), Paragraph(_esc(t["means"]), s_tile_m)]
        foot = []
        if t["asof"]:
            foot.append(("данные на " if t["asof_old"] else "проверено ") + t["asof"])
        if c.source:
            foot.append(c.source)
        if foot:
            inner += [Spacer(1, 8),
                      Paragraph(f"<font color='{'#b45309' if t['asof_old'] else '#16a34a'}'>"
                                f"{_esc(foot[0])}</font>"
                                + (f"&nbsp;&nbsp;<font color='#9aa4b2'>{_esc(foot[1])}</font>"
                                   if len(foot) > 1 else ""), s_faint)]
        cells.append(inner)

    for i in range(0, len(cells), 3):
        row = cells[i:i + 3] + [""] * (3 - len(cells[i:i + 3]))
        t = Table([row], colWidths=[USABLE / 3] * 3)
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), SOFTBG),
            ("ROUNDEDCORNERS", [10, 10, 10, 10]),
            ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(t)
        story.append(Spacer(1, 8))

    # ── периоды владения ────────────────────────────────────────────────────
    if owners:
        section("Периоды владения", "Кто и сколько владел машиной по данным ГИБДД.")
        head = [Paragraph(h, s_label) for h in
                ("ВЛАДЕЛЕЦ", "С КАКОЙ ДАТЫ", "ПО КАКУЮ", "РЕГИСТРАЦИОННОЕ ДЕЙСТВИЕ")]
        rows = [head]
        for o in owners:
            rows.append([
                Paragraph(f"<b>{_esc(o.get('kind') or '—')}</b>", s_row_n),
                Paragraph(_ru_date(o.get("from", "")) or "—", s_row_n),
                Paragraph(_ru_date(o.get("to", "")) or "по настоящее время", s_row_n),
                Paragraph(_esc(o.get("op") or "—"), s_row_n)])
        t = Table(rows, colWidths=[38 * mm, 30 * mm, 36 * mm, 70 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), SOFTBG),
            ("ROUNDEDCORNERS", [10, 10, 10, 10]),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#e7eaee")),
            ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        story.append(t)

    # ── ДТП со схемой ───────────────────────────────────────────────────────
    dtp = next((c for c in checks if c.key == "dtp"), None)
    dtp_items = list(getattr(dtp, "items", None) or []) if dtp else []
    if dtp_items:
        section("Дорожно-транспортные происшествия",
                "Записи ГИБДД. В базу попадают только оформленные аварии — "
                "ремонт без ГИБДД в ней не появится.")
        for idx, it in enumerate(dtp_items):
            det = [Paragraph(f"<b>ДТП от {_esc(it.get('date') or '—')}</b>", s_tile_t),
                   Spacer(1, 8)]
            pairs = [("Тип", it.get("type")), ("Регион", it.get("region")),
                     ("Автомобиль", " ".join(x for x in (it.get("marka"), it.get("model")) if x))]
            prows = [[Paragraph(_esc(k), st("k", 8.5, 12, FAINT)),
                      Paragraph(f"<b>{_esc(v)}</b>", s_row_n)]
                     for k, v in pairs if v]
            if prows:
                pt = Table(prows, colWidths=[24 * mm, 62 * mm])
                pt.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#e7eaee")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ]))
                det.append(pt)

            img_cell: Any = ""
            path = (schemes or [None])[idx] if schemes and idx < len(schemes) else None
            if path and Path(str(path)).exists():
                try:
                    img = Image(str(path))
                    ratio = img.imageHeight / float(img.imageWidth or 1)
                    img.drawWidth = 62 * mm
                    img.drawHeight = 62 * mm * ratio
                    img_cell = [img, Spacer(1, 6),
                                Paragraph(
                                    "<font color='#eab308'>●</font> слабые повреждения&nbsp;&nbsp;"
                                    "<font color='#dc2626'>●</font> сильные&nbsp;&nbsp;"
                                    "<font color='#2563EB'>●</font> степень неизвестна", s_faint)]
                except Exception:  # noqa: BLE001 — картинка не стоит отчёта
                    img_cell = ""

            card = Table([[det, img_cell]], colWidths=[USABLE * 0.55, USABLE * 0.45])
            card.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), SOFTBG),
                ("ROUNDEDCORNERS", [10, 10, 10, 10]),
                ("LEFTPADDING", (0, 0), (-1, -1), 13), ("RIGHTPADDING", (0, 0), (-1, -1), 13),
                ("TOPPADDING", (0, 0), (-1, -1), 13), ("BOTTOMPADDING", (0, 0), (-1, -1), 13),
            ]))
            story.append(KeepTogether(card))
            story.append(Spacer(1, 8))

    # ── таможня ─────────────────────────────────────────────────────────────
    cust = next((c for c in checks if c.key == "customs"), None)
    cust_items = list(getattr(cust, "items", None) or []) if cust else []
    if cust_items:
        section("Таможенное оформление", "Данные ФТС по ввозу автомобиля.")
        head = [Paragraph(h, s_label) for h in
                ("ДАТА ОФОРМЛЕНИЯ", "МОДЕЛЬ ПО ДЕКЛАРАЦИИ", "СТРАНА ВЫВОЗА",
                 "НОМЕР КУЗОВА ПО ГТД")]
        rows = [head]
        for it in cust_items:
            body_no = str(it.get("BodyNumber") or "—")
            # Расхождение номера кузова с VIN — признак «конструктора», на экране
            # оно красное; здесь тоже, иначе в файле пропадает главный сигнал.
            mism = bool(object_ref and body_no not in ("—", "") and
                        body_no.replace("*", "") != str(object_ref).replace("*", ""))
            rows.append([
                Paragraph(f"<b>{_ru_date(it.get('ReleaseDate', '')) or '—'}</b>", s_row_n),
                Paragraph(_esc(it.get("MarkaModel") or "—"), s_row_n),
                Paragraph(_esc(it.get("CountryShort") or it.get("Country") or "не указана"),
                          s_row_n),
                Paragraph(f"<b>{_esc(body_no)}</b>", st("bn", 9.5, 12.5, BAD if mism else INK))])
        t = Table(rows, colWidths=[34 * mm, 50 * mm, 36 * mm, 54 * mm])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), SOFTBG),
            ("ROUNDEDCORNERS", [10, 10, 10, 10]),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#e7eaee")),
            ("LEFTPADDING", (0, 0), (-1, -1), 11), ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        story.append(t)

    # ── рекомендации и дисклеймер ───────────────────────────────────────────
    if verdict.recommendations:
        section("Что сделать перед сделкой")
        for r in verdict.recommendations:
            story.append(Paragraph(
                f"<font color='#9aa4b2'>—</font>&nbsp; {_esc(r)}", s_body))
            story.append(Spacer(1, 5))

    story.append(Spacer(1, 20))
    story.append(Paragraph(
        "Отчёт представляет собой предварительный скрининг рисков по открытым "
        "официальным источникам и не является юридическим заключением или "
        "юридической услугой. Результаты носят информационный характер. Для "
        "окончательного решения по сделке рекомендуем консультацию юриста. "
        "Данные, отмеченные как «нет данных», требуют ручной проверки на "
        "порталах-источниках.", s_muted))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path
