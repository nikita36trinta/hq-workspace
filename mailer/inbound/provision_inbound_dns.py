#!/usr/bin/env python3
"""
DNS для входящей почты (самохостинг приёмника) через Reg.ru API. Идемпотентно.

Добавляет:
  A   mail.<domain>   -> IP сервера-приёмника
  A   inbox.<domain>  -> IP (для Traefik-админки + LE-сертификата)
  MX  @               -> mail.<domain>  (priority 10)

ENV: REGRU_EMAIL, REGRU_PASSWORD, DOMAIN, SERVER_IP (IP приёмника),
     MX_HOST (по умолчанию mail.<domain>), MX_PRIO (10).
Запускать на whitelisted-хосте (Reg.ru API IP-ограничен).
"""
import os, json, urllib.request, urllib.parse

REGRU = "https://api.reg.ru/api/regru2"
DOMAIN = os.environ["DOMAIN"].strip().lower()
IP = os.environ["SERVER_IP"].strip()
MX_HOST = os.environ.get("MX_HOST", f"mail.{DOMAIN}").strip().rstrip(".").lower()
MX_PRIO = int(os.environ.get("MX_PRIO", "10"))


def regru(method, input_data):
    data = urllib.parse.urlencode({
        "username": os.environ["REGRU_EMAIL"],
        "password": os.environ["REGRU_PASSWORD"],
        "input_data": json.dumps(input_data, ensure_ascii=False),
        "input_format": "json",
        "output_content_type": "plain",
    }).encode()
    return json.loads(urllib.request.urlopen(REGRU + "/" + method, data=data, timeout=60).read())


def dom_result(r):
    """Reg.ru всегда отдаёт верхний result=success; реальный статус — на уровне домена."""
    try:
        d = r["answer"]["domains"][0]
        return d.get("result"), d.get("error_text")
    except Exception:
        return r.get("result"), r.get("error_text")


def rrs():
    r = regru("zone/get_resource_records", {"domains": [{"dname": DOMAIN}]})
    out = []
    try:
        for d in r["answer"]["domains"]:
            out += d.get("rrs", [])
    except Exception:
        pass
    return out


def has_a(sub, ip):
    return any(x.get("rectype") == "A" and (x.get("subname") in (sub, f"{sub}.{DOMAIN}"))
               and (x.get("content") or "").strip() == ip for x in CUR)


def has_mx(host):
    h = host.rstrip(".").lower()
    return any(x.get("rectype") == "MX"
               and h in (x.get("content") or "").rstrip(".").lower() for x in CUR)


CUR = rrs()
plan = []
# ВНИМАНИЕ: в Reg.ru A-запись добавляется методом zone/add_alias (ipaddr),
# а zone/add_cname — для CNAME (canonical_name). Метода add_a НЕ существует.
if not has_a("mail", IP):
    plan.append(("zone/add_alias", {"domains": [{"dname": DOMAIN}], "subdomain": "mail", "ipaddr": IP},
                 f"A mail.{DOMAIN} -> {IP}"))
if not has_a("inbox", IP):
    plan.append(("zone/add_alias", {"domains": [{"dname": DOMAIN}], "subdomain": "inbox", "ipaddr": IP},
                 f"A inbox.{DOMAIN} -> {IP}"))
if not has_mx(MX_HOST):
    plan.append(("zone/add_mx", {"domains": [{"dname": DOMAIN}], "subdomain": "@",
                 "mail_server": MX_HOST, "priority": MX_PRIO},
                 f"MX @ -> {MX_HOST} (prio {MX_PRIO})"))

print(f"== inbound DNS {DOMAIN} (IP {IP}) ==")
for method, data, label in plan:
    res, err = dom_result(regru(method, data))
    print(f"   + {label}: {res}" + (f"  [{err}]" if res != "success" else ""))
if not plan:
    print("   все записи уже на месте")
print("Готово. Пропагация Reg.ru — минуты–часы. Проверка: dig MX", DOMAIN, "и dig A", MX_HOST)
