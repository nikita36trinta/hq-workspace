#!/usr/bin/env python3
"""
Провижн tracking-домена (домена ссылок) Unisender Go: делегирование субдомена через NS.

Unisender для домена ссылок просит делегировать субдомен на свои NS
(uns1/uns2/uns3.unisender.com) — это предпочтительный вариант (лучше для доставки
в mail.ru/yandex, чем CNAME-альтернатива). Здесь добавляем 3 NS-записи в зону
родительского домена через Reg.ru API. Идемпотентно.

ENV: REGRU_EMAIL, REGRU_PASSWORD, DOMAIN (напр. chistasdelka.ru),
     SUBDOMAIN (напр. track), NS (через запятую; по умолчанию 3 сервера Unisender).
Reg.ru API IP-whitelisted → запускать НА whitelisted-хосте (см. provision.sh).
"""
import os, json, urllib.request, urllib.parse

REGRU = "https://api.reg.ru/api/regru2"
DOMAIN = os.environ["DOMAIN"].strip().lower()
SUBDOMAIN = os.environ.get("SUBDOMAIN", "track").strip().lower()
NS = [s.strip().rstrip(".").lower()
      for s in os.environ.get("NS", "uns1.unisender.com,uns2.unisender.com,uns3.unisender.com").split(",")
      if s.strip()]
FQDN = f"{SUBDOMAIN}.{DOMAIN}"


def regru(method, input_data):
    data = urllib.parse.urlencode({
        "username": os.environ["REGRU_EMAIL"],
        "password": os.environ["REGRU_PASSWORD"],
        "input_data": json.dumps(input_data, ensure_ascii=False),
        "input_format": "json",
        "output_content_type": "plain",
    }).encode()
    return json.loads(urllib.request.urlopen(REGRU + "/" + method, data=data, timeout=60).read())


def existing_rrs():
    r = regru("zone/get_resource_records", {"domains": [{"dname": DOMAIN}]})
    out = []
    try:
        for d in r["answer"]["domains"]:
            out += d.get("rrs", [])
    except Exception:
        pass
    return out


def main():
    print(f"== Tracking-домен {FQDN} → NS {NS} ==")
    rrs = existing_rrs()
    # Уже делегированные NS для нашего субдомена (Reg.ru отдаёт rr с prefix субдомена)
    have = set()
    for rr in rrs:
        if rr.get("rectype") == "NS":
            fqdn = rr.get("subname") or rr.get("prefix") or ""
            val = (rr.get("content") or rr.get("value") or "").rstrip(".").lower()
            # subname может быть "track" или "track.chistasdelka.ru"
            if fqdn in (SUBDOMAIN, FQDN):
                have.add(val)
    print(f"   уже есть NS для {SUBDOMAIN}: {sorted(have) or '—'}")

    added = 0
    for ns in NS:
        if ns in have:
            print(f"   = {ns} уже делегирован — пропуск")
            continue
        r = regru("zone/add_ns", {
            "domains": [{"dname": DOMAIN}],
            "subdomain": SUBDOMAIN,
            "dns_server": ns,
        })
        ok = r.get("result") == "success"
        print(f"   + NS {SUBDOMAIN} → {ns}: {r.get('result', r)}"
              + ("" if ok else f"  {json.dumps(r, ensure_ascii=False)[:200]}"))
        added += 1 if ok else 0

    print(f"Готово. Добавлено NS: {added}. Проверь статус в Unisender (Домены ссылок) — "
          f"активация после пропагации (обычно минуты–часы, до 48ч).")


main()
