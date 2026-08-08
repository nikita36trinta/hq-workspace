"""Транзакционные email через Unisender Go (вендорено из hq-workspace/mailer).
Самодостаточно (urllib). Ключ — ENV UNISENDER_GO_API_KEY. Никогда не бросает наружу.
"""
import base64, os, json, urllib.request, urllib.error

SEND_URL = os.getenv("UNISENDER_GO_SEND_URL",
                     "https://go2.unisender.ru/ru/transactional/api/v1/email/send.json")


def send_email(to, subject, html, from_email, from_name="", text=None,
               api_key=None, tag=None, reply_to=None, reply_to_name=None,
               attachments=None):
    """attachments — список (имя_файла, путь_на_диске). Вложение важнее ссылки:
    встроенные браузеры почтовых приложений то не скачивают файл по ссылке, то
    открывают пустую вкладку, и человек с оплаченным отчётом остаётся ни с чем
    (жалобы по 1bc9a30808d7, 5ec0b63a62ef, 31f0494c35d8 за один день). Файл,
    пришедший письмом, открывается всегда и не зависит ни от какого браузера.
    Ошибка чтения файла НЕ отменяет письмо — ссылка в нём всё равно есть."""
    key = api_key or os.environ.get("UNISENDER_GO_API_KEY")
    if not key:
        return {"status": "error", "note": "no UNISENDER_GO_API_KEY"}
    message = {
        "recipients": [{"email": to}],
        "subject": subject,
        "from_email": from_email,
        "from_name": from_name,
        "body": {"html": html},
    }
    if text:
        message["body"]["plaintext"] = text
    files = []
    for name, path in (attachments or []):
        try:
            with open(path, "rb") as f:
                files.append({"type": "application/pdf", "name": name,
                              "content": base64.b64encode(f.read()).decode()})
        except Exception:  # noqa: BLE001 — без вложения письмо всё равно нужно отправить
            pass
    if files:
        message["attachments"] = files
    if tag:
        message["tags"] = [tag]
    reply_to = reply_to or os.getenv("EMAIL_REPLY_TO")
    if reply_to:
        message["reply_to"] = reply_to
        if reply_to_name or from_name:
            message["reply_to_name"] = reply_to_name or from_name
    req = urllib.request.Request(
        SEND_URL, data=json.dumps({"message": message}).encode(),
        headers={"X-API-KEY": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"status": "error", "http": e.code}
    except Exception as e:
        return {"status": "error", "note": str(e)[:120]}
