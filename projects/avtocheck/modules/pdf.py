"""
PDF-отчёт «ЧистаяСделка» через reportlab.

Использует DejaVuSans.ttf (лежит рядом), чтобы корректно рендерить кириллицу.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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
    # fallback: reportlab default (кириллица не будет отображаться, но не упадём)
    return "Helvetica"


RISK_COLOR = {
    RISK_LOW: colors.HexColor("#16a34a"),
    RISK_MEDIUM: colors.HexColor("#d97706"),
    RISK_HIGH: colors.HexColor("#dc2626"),
}

STATUS_ICON = {
    "not_found": "✓",
    "found": "⚠",
    "not_checked": "?",
    # Блок не входит в базовый тариф. Без этой строки в платный PDF уходило
    # английское слово «locked» — так и было напечатано в графе «Результат».
    "locked": "·",
}
# Подписи относятся к ПРЕДМЕТУ ПОИСКА (обременения, аресты, производства), а не к
# объекту: «не найдено» рядом с фразой «объект найден в ЕГРН» читается как
# противоречие — на этом мы уже получили требование возврата. Формулировки
# совпадают с modules/verdict.py, чтобы бейдж и текст вердикта не расходились.
STATUS_LABEL = {
    "not_found": "чисто",
    "found": "обнаружено",
    "not_checked": "не проверено",
    "locked": "не входит в тариф",
}


def render_report(
    out_path: str,
    *,
    report_id: str,
    seller_name: str,
    seller_dob: str,
    object_ref: str,
    email: str,
    checks: list[Check],
    verdict: Verdict,
    addon: dict | None = None,
    deal_kit: dict | None = None,
) -> str:
    font = _register_font()

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        name="Title",
        parent=styles["Title"],
        fontName=font,
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#0a0a0a"),
    )
    h2 = ParagraphStyle(
        name="H2",
        parent=styles["Heading2"],
        fontName=font,
        fontSize=14,
        leading=18,
        spaceBefore=8,
        spaceAfter=6,
        textColor=colors.HexColor("#0a0a0a"),
    )
    body = ParagraphStyle(
        name="Body",
        parent=styles["BodyText"],
        fontName=font,
        fontSize=10.5,
        leading=15,
        textColor=colors.HexColor("#1f2937"),
    )
    muted = ParagraphStyle(
        name="Muted",
        parent=body,
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#6b7280"),
    )
    risk_style = ParagraphStyle(
        name="Risk",
        parent=styles["Title"],
        fontName=font,
        fontSize=28,
        leading=32,
        alignment=1,  # center
        textColor=RISK_COLOR.get(verdict.risk, colors.black),
    )
    rec_style = ParagraphStyle(
        name="Rec",
        parent=body,
        leftIndent=12,
        bulletIndent=0,
        spaceAfter=4,
    )

    # ---- палитра / ширины ----
    BRAND = colors.HexColor("#0f2440")   # тёмно-графитовый (премиальный, «юридический»)
    ACCENT = colors.HexColor("#2563eb")  # синий акцент — секции, лого
    LINE = colors.HexColor("#e2e8f0")
    USABLE = 174 * mm
    rc = RISK_COLOR.get(verdict.risk, colors.HexColor("#0a0a0a"))
    RISK_TINT = {
        RISK_LOW: colors.HexColor("#ecfdf3"),
        RISK_MEDIUM: colors.HexColor("#fef6e7"),
        RISK_HIGH: colors.HexColor("#fdecec"),
    }
    rtint = RISK_TINT.get(verdict.risk, colors.HexColor("#f1f5f9"))

    def _esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _rich(s: str) -> str:
        """То же, но с сохранением <b> — формулировки последствий выделяют
        главное жирным, и на сайте, и в файле это должно совпадать."""
        return _esc(s).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")

    # доп. стили
    white_title = ParagraphStyle("wt", parent=title_style, textColor=colors.white,
                                 fontSize=21, leading=24)
    white_sub = ParagraphStyle("ws", parent=muted, textColor=colors.HexColor("#dbeafe"),
                               fontSize=9.5, leading=12)
    b_label = ParagraphStyle("bl", fontName=font, fontSize=8.5, leading=11,
                             textColor=colors.HexColor("#64748b"))
    b_risk = ParagraphStyle("br", fontName=font, fontSize=28, leading=31,
                            textColor=rc)
    b_head = ParagraphStyle("bh", fontName=font, fontSize=11.5, leading=15,
                            textColor=colors.HexColor("#334155"))
    h2a = ParagraphStyle("h2a", parent=h2, textColor=ACCENT, fontSize=13,
                         spaceBefore=2, spaceAfter=2)
    cell = ParagraphStyle("cell", parent=body, fontName=font, fontSize=9, leading=12)
    cell_res = ParagraphStyle("cell_res", parent=cell, textColor=colors.HexColor("#475569"))
    rec_box = ParagraphStyle("recbox", parent=body, fontSize=10, leading=14, spaceAfter=6)

    def section(title: str) -> list:
        return [Spacer(1, 12), Paragraph(title, h2a),
                HRFlowable(width=USABLE, thickness=1.2, color=ACCENT,
                           spaceBefore=3, spaceAfter=8, lineCap="round")]

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont(font, 8)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawString(18 * mm, 12 * mm, "ЧистаяСделка Авто · avto.chistasdelka.ru")
        canvas.drawRightString(192 * mm, 12 * mm, f"Отчёт {report_id} · стр. {doc_.page}")
        canvas.setStrokeColor(LINE)
        canvas.line(18 * mm, 15 * mm, 192 * mm, 15 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=20 * mm,
        title="ЧистаяСделка — риск-отчёт",
    )

    story: list = []

    # --- брендовый хедер (цветная плашка) ---
    head = Table([[Paragraph("Чистая<font color='#93c5fd'>Сделка</font>", white_title)],
                  [Paragraph("Проверка автомобиля перед покупкой по VIN", white_sub)]],
                 colWidths=[USABLE])
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 16), ("RIGHTPADDING", (0, 0), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (0, 0), 16), ("BOTTOMPADDING", (0, 0), (0, 0), 1),
        ("TOPPADDING", (0, 1), (0, 1), 0), ("BOTTOMPADDING", (0, 1), (0, 1), 16),
    ]))
    story.append(head)
    story.append(Spacer(1, 14))

    # --- цветной риск-баннер ---
    banner = Table([[Paragraph("УРОВЕНЬ РИСКА ПРИ ПОКУПКЕ", b_label)],
                    [Paragraph(verdict.risk.upper(), b_risk)],
                    [Paragraph(_esc(verdict.headline), b_head)]],
                   colWidths=[USABLE])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), rtint),
        ("LINEBEFORE", (0, 0), (0, -1), 4, rc),   # толстая цветная грань слева
        ("LEFTPADDING", (0, 0), (-1, -1), 18), ("RIGHTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING", (0, 0), (0, 0), 14), ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("TOPPADDING", (0, 1), (0, 1), 0), ("BOTTOMPADDING", (0, 1), (0, 1), 4),
        ("TOPPADDING", (0, 2), (0, 2), 0), ("BOTTOMPADDING", (0, 2), (0, 2), 14),
    ]))
    story.append(banner)

    # --- заключение (LLM/правила) ---
    story += section("Заключение")
    story.append(Paragraph(_esc(verdict.body), body))
    if verdict.llm_used:
        story.append(Spacer(1, 4))
        story.append(Paragraph("Заключение подготовлено с помощью ИИ на основе данных госреестров.", muted))

    # --- метаданные объекта ---
    # Строку продавца показываем, только если он вообще есть. По автомобилю ФИО
    # не собирается, и «Продавец —  Дата рождения —» занимало треть блока пустыми
    # прочерками, намекая, что проверку чего-то не доделали.
    story += section("Автомобиль" if not seller_name else "Объект и продавец")
    meta_data = [
        [Paragraph("Отчёт №", cell_res), Paragraph(_esc(report_id), cell),
         Paragraph("Дата", cell_res), Paragraph(dt.datetime.now().strftime("%d.%m.%Y %H:%M"), cell)],
    ]
    if seller_name:
        meta_data.append(
            [Paragraph("Продавец", cell_res), Paragraph(_esc(seller_name), cell),
             Paragraph("Дата рождения", cell_res), Paragraph(_esc(seller_dob or "—"), cell)])
    meta_data.append(
        [Paragraph("VIN / госномер" if not seller_name else "Объект", cell_res),
         Paragraph(_esc(object_ref or "—"), cell),
         Paragraph("Email", cell_res), Paragraph(_esc(email or "—"), cell)])
    meta_table = Table(meta_data, colWidths=[26 * mm, 61 * mm, 26 * mm, 61 * mm])
    meta_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
    ]))
    story.append(meta_table)

    tint_by_status = {
        "found": colors.HexColor("#fffbeb"),
        "not_found": colors.HexColor("#f0fdf4"),
        "not_checked": colors.HexColor("#f8fafc"),
        "locked": colors.HexColor("#f8fafc"),
    }
    hex_by_status = {"found": "#b45309", "not_found": "#15803d",
                     "not_checked": "#64748b", "locked": "#64748b"}

    def _checks_table(check_list: list) -> Table:
        # Цвет задаём в самом абзаце. TEXTCOLOR у TableStyle на Paragraph не
        # действует — стиль абзаца сильнее, и шапка печаталась тёмным по тёмному:
        # три слова заголовка были фактически невидимы в платном документе.
        head_style = ParagraphStyle("cell_hd", parent=cell, textColor=colors.white)
        header_row = [Paragraph("<b>Проверка</b>", head_style),
                      Paragraph("<b>Источник</b>", head_style),
                      Paragraph("<b>Результат</b>", head_style)]
        rows = [header_row]
        for c in check_list:
            icon = STATUS_ICON.get(c.status, "?")
            result = STATUS_LABEL.get(c.status, c.status)
            hx = hex_by_status.get(c.status, "#0a0a0a")
            name_html = f'<font color="{hx}"><b>{icon}</b></font>&nbsp; {_esc(c.name)}'
            res_html = (f'<font color="{hx}"><b>{_esc(result)}</b></font>'
                        f'<br/><font size="8">{_esc(c.detail)}</font>')
            rows.append([Paragraph(name_html, cell), Paragraph(_esc(c.source), cell),
                         Paragraph(res_html, cell_res)])
        tbl = Table(rows, colWidths=[56 * mm, 40 * mm, 78 * mm], repeatRows=1)
        ts = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ])
        for i, c in enumerate(check_list, start=1):
            ts.add("BACKGROUND", (0, i), (-1, i), tint_by_status.get(c.status, colors.white))
        tbl.setStyle(ts)
        return tbl

    # --- таблица проверок (базовые: объект + ФССП) ---
    story += section("Результаты проверок")
    story.append(_checks_table(checks))

    # --- углублённая проверка по ИНН (доплаченная доп-услуга) ---
    addon_checks: list = []
    if addon and addon.get("checks"):
        story += section("Углублённая проверка по ИНН")
        addon_checks = [
            Check(key=c.get("key", ""), name=c.get("name", ""), source=c.get("source", ""),
                  status=c.get("status", ""), detail=c.get("detail", ""), items=c.get("items", []))
            for c in addon["checks"]
        ]
        story.append(_checks_table(addon_checks))
        if addon.get("analysis"):
            story.append(Spacer(1, 8))
            story.append(Paragraph("<b>Вывод юриста по ИНН:</b> " + _esc(addon["analysis"]), body))

    # --- правовые последствия ---
    # Тот же справочник, что на странице отчёта: сайт и файл обязаны говорить
    # одно и то же, иначе клиент показывает юристу PDF, а там пусто.
    try:
        from . import legal
        cons = legal.consequences(list(checks) + list(addon_checks))
    except Exception as exc:                                   # noqa: BLE001
        print(f"[pdf] последствия не собраны: {type(exc).__name__}: {exc}", flush=True)
        cons = []

    if cons:
        story += section("Правовые последствия при совершении сделки")
        c_num = ParagraphStyle("cnum", parent=cell, fontSize=10, leading=13,
                               alignment=1)
        c_txt = ParagraphStyle("ctxt", parent=cell, fontSize=9.5, leading=13)
        c_law = ParagraphStyle("claw", parent=cell, fontSize=8, leading=11,
                               textColor=colors.HexColor("#8a92a0"), spaceBefore=2)
        rows, styles_extra = [], []
        for i, con in enumerate(cons):
            sev_hex = "#b91c1c" if con.severity == "block" else "#b45309"
            sev = colors.HexColor(sev_hex)
            num = Paragraph(f'<font color="{sev_hex}"><b>{i + 1}</b></font>', c_num)
            parts = [Paragraph(_rich(con.text), c_txt)]
            if con.norms:
                links = " · ".join(
                    f'<link href="{n.url}" color="#2563eb">{_esc(n.label)}</link>'
                    + (f' <font color="#a8b0bd">{_esc(n.note)}</font>' if n.note else "")
                    for n in con.norms
                )
                parts.append(Paragraph(links, c_law))
            rows.append([num, parts])
            styles_extra.append(("LINEBEFORE", (0, i), (0, i), 2.5, sev))
        tbl = Table(rows, colWidths=[9 * mm, USABLE - 9 * mm])
        ts = TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbfaf7")),
            ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ])
        for extra in styles_extra:
            ts.add(*extra)
        tbl.setStyle(ts)
        story.append(tbl)
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "Нормы приведены в редакции на дату заключения; ссылки ведут на текст "
            "закона. Перечислены только последствия с выверенной формулировкой.", muted))

    # --- пакет к сделке ---
    # Тот же набор, что на странице отчёта: человек несёт к сделке ФАЙЛ, а не
    # вкладку браузера, и список документов нужен ему именно там. Приходит уже
    # готовым из modules/dealkit — PDF ничего не решает, только рисует.
    if deal_kit and (deal_kit.get("ask_seller") or deal_kit.get("check_self")):
        story += section("Пакет к сделке")
        k_title = ParagraphStyle("ktitle", parent=cell, fontSize=9.5, leading=13)
        k_why = ParagraphStyle("kwhy", parent=cell, fontSize=8.5, leading=12,
                               textColor=colors.HexColor("#475569"), spaceBefore=3)
        k_law = ParagraphStyle("klaw", parent=cell, fontSize=8, leading=11,
                               textColor=colors.HexColor("#64748b"), spaceBefore=2)

        def _kit_block(caption: str, items: list) -> None:
            if not items:
                return
            story.append(Paragraph(f"<b>{_esc(caption)}</b>", body))
            story.append(Spacer(1, 5))
            rows, extra = [], []
            for i, it in enumerate(items):
                urgent = bool(it.get("urgent"))
                mark = "!" if urgent else "•"
                hexc = "#b45309" if urgent else "#94a3b8"
                num = Paragraph(f'<font color="{hexc}"><b>{mark}</b></font>',
                                ParagraphStyle("kn", parent=cell, fontSize=10,
                                               leading=13, alignment=1))
                parts = [Paragraph(_esc(str(it.get("title") or "")), k_title),
                         Paragraph(_esc(str(it.get("why") or "")), k_why)]
                if it.get("law"):
                    parts.append(Paragraph(_esc(str(it["law"])), k_law))
                rows.append([num, parts])
                if urgent:
                    extra.append(("LINEBEFORE", (0, i), (0, i), 2.5,
                                  colors.HexColor("#d97706")))
            t = Table(rows, colWidths=[7 * mm, USABLE - 7 * mm])
            st = TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbfaf7")),
                ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
            ])
            for e in extra:
                st.add(*e)
            t.setStyle(st)
            story.append(t)
            story.append(Spacer(1, 10))

        _kit_block("Запросить у продавца", deal_kit.get("ask_seller") or [])
        _kit_block("Проверить самому — эти сведения реестры нам не отдают",
                   deal_kit.get("check_self") or [])

        if deal_kit.get("day_of"):
            story.append(Paragraph("<b>В день сделки</b>", body))
            story.append(Spacer(1, 4))
            for line in deal_kit["day_of"]:
                story.append(Paragraph("•&nbsp; " + _esc(str(line)), rec_box))
            story.append(Spacer(1, 8))

        letters = deal_kit.get("letters") or {}
        for name, text in letters.items():
            story.append(Paragraph(f"<b>Письмо {_esc(str(name))}</b>", body))
            story.append(Spacer(1, 4))
            # Перевод строки в тексте письма — это абзац, иначе reportlab склеит
            # всё в кашу и скопировать список документов станет невозможно.
            for line in str(text).split("\n"):
                story.append(Paragraph(_esc(line) if line.strip() else "&nbsp;", k_why))
            story.append(Spacer(1, 8))

        story.append(Paragraph(
            "Это перечень того, что стоит проверить, собранный по данным реестров, "
            "а не юридическая консультация.", muted))

    # --- рекомендации ---
    story += section("Рекомендации")
    for rec in verdict.recommendations:
        story.append(Paragraph("•&nbsp; " + _esc(rec), rec_box))

    # --- дисклеймер ---
    story += section("Дисклеймер")
    story.append(Paragraph(
        "Отчёт представляет собой предварительный скрининг рисков по открытым "
        "официальным источникам и не является юридическим заключением или "
        "юридической услугой. Результаты носят информационный характер. Для "
        "окончательного решения по сделке рекомендуем консультацию юриста. "
        "Данные, отмеченные как «не проверено», требуют ручной проверки на "
        "порталах-источниках.", muted))

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return out_path
