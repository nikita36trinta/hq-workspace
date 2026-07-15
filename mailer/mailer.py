#!/usr/bin/env python3
"""
Переиспользуемый отправитель транзакционных писем через Unisender Go.

Импортируй в любом проекте:
    from mailer import send_email
    send_email(to="user@ya.ru", subject="Ваш отчёт готов",
               html="<p>…<a href='https://…/report/x/view'>Открыть отчёт</a></p>",
               from_email="noreply@chistasdelka.ru", from_name="ЧистаяСделка")

Ключ — из ENV UNISENDER_GO_API_KEY (или передать api_key=). Отправка НЕ IP-ограничена
(работает с любого сервера/проекта). Возвращает JSON-ответ Unisender Go.
"""
import os, json, urllib.request, urllib.error

SEND_URL = os.getenv("UNISENDER_GO_SEND_URL",
                     "https://go2.unisender.ru/ru/transactional/api/v1/email/send.json")


def send_email(to, subject, html, from_email, from_name="", text=None,
               api_key=None, tag=None, reply_to=None, reply_to_name=None):
    key = api_key or os.environ["UNISENDER_GO_API_KEY"]
    message = {
        "recipients": [{"email": to}],
        "subject": subject,
        "from_email": from_email,
        "from_name": from_name,
        "body": {"html": html},
    }
    if text:
        message["body"]["plaintext"] = text
    if tag:
        message["tags"] = [tag]
    # Reply-To → наш самописный приёмник (hello@/support@), чтобы ответы клиентов не терялись.
    reply_to = reply_to or os.getenv("EMAIL_REPLY_TO")
    if reply_to:
        message["reply_to"] = reply_to
        if reply_to_name or from_name:
            message["reply_to_name"] = reply_to_name or from_name
    req = urllib.request.Request(SEND_URL,
        data=json.dumps({"message": message}).encode(),
        headers={"X-API-KEY": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"status": "error", "http": e.code}


if __name__ == "__main__":
    import sys
    # ./mailer.py to@ya.ru "Тема" "<p>html</p>" from@domain "Имя"
    print(send_email(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4],
                     sys.argv[5] if len(sys.argv) > 5 else ""))
