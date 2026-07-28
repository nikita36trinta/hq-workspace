#!/usr/bin/env python3
"""
Самописный входящий SMTP-приёмник (без внешних сервисов).

Слушает :25, принимает почту ТОЛЬКО для наших адресов @<домен> (whitelist локальных
частей), всё остальное отбивает 550 → ноль релея, ноль приёма спама «на кого попало».
Каждое принятое письмо сохраняется локально: сырой .eml + строка в index.jsonl.
Читается мини-админкой inbox_admin.py.

ENV:
  MAILIN_DOMAIN   — наш домен (chistasdelka.ru)
  MAILIN_ALLOWED  — разрешённые локальные части через запятую (hello,support,noreply,info)
  MAILIN_STORE    — каталог хранилища (/data/store)
  MAILIN_MAX_SIZE — лимит размера письма в байтах (по умолчанию 15 МБ)
  MAILIN_TG_TOKEN — токен телеграм-бота для уведомлений (пусто = не уведомлять)
  MAILIN_TG_CHAT  — chat_id получателя уведомлений
  MAILIN_TG_PROXY — прокси до api.telegram.org (РФ-серверы туда напрямую не ходят)
  MAILIN_TG_SKIP  — локальные части, о которых НЕ уведомлять (по умолчанию noreply)
"""
import os, json, uuid, datetime, time, threading, urllib.request
from email.parser import BytesParser
from email.policy import default as default_policy
from aiosmtpd.controller import Controller

STORE = os.environ.get("MAILIN_STORE", "/data/store")
EML_DIR = os.path.join(STORE, "eml")
INDEX = os.path.join(STORE, "index.jsonl")
# Мультидоменный: один приёмник на :25 обслуживает список доменов (MAILIN_DOMAINS,
# через запятую). MAILIN_DOMAIN оставлен для обратной совместимости.
DOMAINS = {d.strip().lower() for d in
           os.environ.get("MAILIN_DOMAINS", os.environ.get("MAILIN_DOMAIN", "chistasdelka.ru")).split(",")
           if d.strip()}
ALLOWED = {x.strip().lower() for x in
           os.environ.get("MAILIN_ALLOWED", "hello,support,noreply,info").split(",") if x.strip()}
MAX_SIZE = int(os.environ.get("MAILIN_MAX_SIZE", str(15 * 1024 * 1024)))

# ---- уведомления в телеграм ----
# Без них ящик — это папка на диске, куда никто не заходит: три обращения клиентов
# (включая требование вернуть деньги) пролежали непрочитанными по четверо суток.
TG_TOKEN = os.environ.get("MAILIN_TG_TOKEN", "").strip()
TG_CHAT = os.environ.get("MAILIN_TG_CHAT", "").strip()
TG_PROXY = os.environ.get("MAILIN_TG_PROXY", "").strip()
# Свои же служебные письма (noreply@) сыплются на support@ пачками при каждом тесте —
# уведомлять о них значит приучить себя не смотреть на уведомления.
TG_SKIP = {x.strip().lower() for x in os.environ.get("MAILIN_TG_SKIP", "noreply").split(",") if x.strip()}


def _tg_send(text: str) -> None:
    if not (TG_TOKEN and TG_CHAT):
        return
    opener = (urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": TG_PROXY, "https": TG_PROXY}))
        if TG_PROXY else urllib.request.build_opener())
    body = json.dumps({"chat_id": TG_CHAT, "text": text[:4000],
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
        data=body, headers={"Content-Type": "application/json"})
    try:
        opener.open(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001 — уведомление не имеет права мешать приёму почты
        print("[tg] не отправлено: %s %s" % (type(e).__name__, str(e)[:120]), flush=True)


def _notify(rec: dict) -> None:
    """Письмо → сообщение в телеграм. В отдельном потоке: SMTP-ответ отправителю
    не должен ждать, пока мы сходим наружу через прокси."""
    sender = (rec.get("envelope_from") or rec.get("from") or "").lower()
    local = sender.split("@")[0].split("<")[-1].strip()
    if local in TG_SKIP:
        return
    text = ("Письмо на {to}\n\n"
            "От: {frm}\n"
            "Тема: {subj}\n\n"
            "{body}").format(
        to=rec.get("to") or "—",
        frm=rec.get("from") or "—",
        subj=rec.get("subject") or "(без темы)",
        body=(rec.get("preview") or "(пустое тело)")[:900])
    threading.Thread(target=_tg_send, args=(text,), daemon=True).start()

os.makedirs(EML_DIR, exist_ok=True)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Handler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        addr = address.lower().strip().strip("<>")
        local, _, dom = addr.partition("@")
        if dom not in DOMAINS:
            return "550 relaying denied"          # принимаем только наши домены — не релей
        if ALLOWED and local not in ALLOWED:
            return "550 no such mailbox"          # только известные ящики → режем спам
        if len(envelope.rcpt_tos) >= 20:
            return "452 too many recipients"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        data = envelope.content if isinstance(envelope.content, bytes) else envelope.content.encode()
        if len(data) > MAX_SIZE:
            return "552 message too large"
        mid = uuid.uuid4().hex
        with open(os.path.join(EML_DIR, mid + ".eml"), "wb") as f:
            f.write(data)
        subject = from_ = date_ = ""
        body = ""
        has_attach = False
        try:
            msg = BytesParser(policy=default_policy).parsebytes(data)
            subject = str(msg.get("subject", "") or "")
            from_ = str(msg.get("from", "") or envelope.mail_from or "")
            date_ = str(msg.get("date", "") or "")
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_filename():
                        has_attach = True
                    if part.get_content_type() == "text/plain" and not part.get_filename() and not body:
                        try:
                            body = part.get_content()
                        except Exception:
                            pass
            else:
                try:
                    body = msg.get_content()
                except Exception:
                    body = ""
        except Exception:
            subject = "(не удалось разобрать письмо)"
            from_ = envelope.mail_from or ""
        rec = {
            "id": mid, "ts": _now(),
            "from": from_[:300], "to": ",".join(envelope.rcpt_tos)[:300],
            "envelope_from": envelope.mail_from, "peer": str(session.peer),
            "subject": subject[:500], "date": date_[:120],
            "preview": " ".join((body or "").split())[:400],
            "size": len(data), "has_attach": bool(has_attach),
        }
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[recv] {mid} from={rec['from']!r} to={rec['to']!r} "
              f"subj={rec['subject']!r} peer={rec['peer']}", flush=True)
        _notify(rec)
        return "250 Message accepted for delivery"


def main():
    controller = Controller(
        Handler(), hostname="0.0.0.0", port=25,
        data_size_limit=MAX_SIZE, enable_SMTPUTF8=True,
    )
    controller.start()
    print(f"[mailin] SMTP :25 up | domains={sorted(DOMAINS)} | allowed={sorted(ALLOWED)} | store={STORE} "
          f"| телеграм={'да' if (TG_TOKEN and TG_CHAT) else 'нет'}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
