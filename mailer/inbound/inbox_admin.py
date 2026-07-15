#!/usr/bin/env python3
"""
Мини-админка для входящих писем (читает локальное хранилище приёмника).

Список писем + просмотр (безопасно: только text/plain, экранировано; исходник — .eml на скачивание).
Защита — HTTP Basic (INBOX_USER / INBOX_PASS). Никаких внешних сервисов.

ENV: MAILIN_STORE, INBOX_USER, INBOX_PASS
"""
import os, json, html, secrets
from email.parser import BytesParser
from email.policy import default as default_policy
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

STORE = os.environ.get("MAILIN_STORE", "/data/store")
EML_DIR = os.path.join(STORE, "eml")
INDEX = os.path.join(STORE, "index.jsonl")
USER = os.environ.get("INBOX_USER", "admin")
PASS = os.environ.get("INBOX_PASS", "")

app = FastAPI(title="ЧистаяСделка — входящие")
security = HTTPBasic()

CSS = """
*{box-sizing:border-box} body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;
background:#eef1f6;color:#1f2733} a{color:#16375f;text-decoration:none}
.wrap{max-width:900px;margin:0 auto;padding:28px 18px}
h1{font-size:20px;margin:0 0 4px;color:#16375f} .sub{color:#8a94a3;font-size:13px;margin:0 0 20px}
.card{background:#fff;border:1px solid #e2e8f1;border-radius:14px;overflow:hidden}
.row{display:block;padding:14px 18px;border-bottom:1px solid #eef1f6}
.row:last-child{border-bottom:0} .row:hover{background:#f6f8fc}
.from{font-weight:600;color:#151d28;font-size:14px} .subj{font-size:14px;color:#39424f;margin:2px 0}
.prev{font-size:13px;color:#8a94a3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{font-size:12px;color:#aab3c0;float:right} .empty{padding:40px 18px;text-align:center;color:#8a94a3}
.back{display:inline-block;margin-bottom:14px;font-size:13px}
.hdr{background:#fff;border:1px solid #e2e8f1;border-radius:14px;padding:18px}
.hdr b{color:#16375f} .body{background:#fff;border:1px solid #e2e8f1;border-radius:14px;
padding:18px;margin-top:14px;white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.55}
.tag{display:inline-block;background:#eef2f8;color:#5b6675;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}
"""


def _auth(c: HTTPBasicCredentials = Depends(security)):
    ok = bool(PASS) and secrets.compare_digest(c.username, USER) and secrets.compare_digest(c.password, PASS)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized",
                            {"WWW-Authenticate": "Basic"})
    return True


def _index():
    rows = []
    if os.path.exists(INDEX):
        with open(INDEX, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    rows.reverse()
    return rows


def _page(title, body):
    return HTMLResponse(f"<!doctype html><html lang=ru><head><meta charset=utf-8>"
                        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
                        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
                        f"<body><div class=wrap>{body}</div></body></html>")


@app.get("/", response_class=HTMLResponse)
def inbox(_: bool = Depends(_auth)):
    rows = _index()
    if not rows:
        items = "<div class=card><div class=empty>Пока нет входящих писем.</div></div>"
    else:
        items = "<div class=card>"
        for r in rows:
            att = "<span class=tag>вложение</span>" if r.get("has_attach") else ""
            items += (
                f"<a class=row href='/msg/{html.escape(r['id'])}'>"
                f"<span class=meta>{html.escape((r.get('ts') or '')[:16].replace('T',' '))}</span>"
                f"<div class=from>{html.escape(r.get('from') or '—')}</div>"
                f"<div class=subj>{html.escape(r.get('subject') or '(без темы)')}{att}</div>"
                f"<div class=prev>{html.escape(r.get('preview') or '')}</div></a>"
            )
        items += "</div>"
    return _page("Входящие", f"<h1>Входящие</h1><p class=sub>{len(rows)} писем · "
                             f"приёмник chistasdelka.ru</p>{items}")


@app.get("/msg/{mid}", response_class=HTMLResponse)
def message(mid: str, _: bool = Depends(_auth)):
    if not mid.isalnum():
        raise HTTPException(400, "bad id")
    path = os.path.join(EML_DIR, mid + ".eml")
    if not os.path.exists(path):
        raise HTTPException(404, "not found")
    with open(path, "rb") as f:
        msg = BytesParser(policy=default_policy).parsebytes(f.read())
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    body = part.get_content(); break
                except Exception:
                    pass
    else:
        try:
            body = msg.get_content()
        except Exception:
            body = ""
    if not body:
        body = "(в письме нет текстовой части — откройте исходник .eml)"
    hdr = (f"<div class=hdr><div><b>От:</b> {html.escape(str(msg.get('from','—')))}</div>"
           f"<div><b>Кому:</b> {html.escape(str(msg.get('to','—')))}</div>"
           f"<div><b>Тема:</b> {html.escape(str(msg.get('subject','(без темы)')))}</div>"
           f"<div><b>Дата:</b> {html.escape(str(msg.get('date','—')))}</div>"
           f"<div style='margin-top:8px'><a href='/raw/{html.escape(mid)}'>Скачать .eml</a></div></div>")
    return _page("Письмо", f"<a class=back href='/'>← ко входящим</a>{hdr}"
                           f"<div class=body>{html.escape(body)}</div>")


@app.get("/raw/{mid}")
def raw(mid: str, _: bool = Depends(_auth)):
    if not mid.isalnum():
        raise HTTPException(400, "bad id")
    path = os.path.join(EML_DIR, mid + ".eml")
    if not os.path.exists(path):
        raise HTTPException(404, "not found")
    with open(path, "rb") as f:
        return Response(f.read(), media_type="message/rfc822",
                        headers={"Content-Disposition": f"attachment; filename={mid}.eml"})


@app.get("/healthz")
def healthz():
    return {"ok": True}
