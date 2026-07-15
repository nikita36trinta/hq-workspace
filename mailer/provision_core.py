#!/usr/bin/env python3
"""
Провижн домена-отправителя для транзакционных писем: Unisender Go + Reg.ru DNS.

Идемпотентно. Читает из ENV: UNISENDER_GO_API_KEY, REGRU_EMAIL, REGRU_PASSWORD, DOMAIN.
Reg.ru API IP-whitelisted → этот скрипт запускается НА whitelisted-хосте (см. provision.sh).

Точные записи Unisender Go:
  verification  TXT   @               unisender-go-validate-hash=...
  DKIM          TXT   us._domainkey   v=DKIM1; k=rsa; p=<key>
  DMARC         CNAME _dmarc          <domain>.dmarc.unisender.ru.
  SPF           TXT   @               v=spf1 include:spf.unisender.ru ~all  (мержить с существующим!)
"""
import os, json, time, urllib.request, urllib.parse, urllib.error

UNI = "https://go2.unisender.ru/ru/transactional/api/v1"
REGRU = "https://api.reg.ru/api/regru2"
DOMAIN = os.environ["DOMAIN"].strip().lower()


def uni(method, payload):
    req = urllib.request.Request(f"{UNI}/{method}",
        data=json.dumps(payload).encode(),
        headers={"X-API-KEY": os.environ["UNISENDER_GO_API_KEY"], "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:  # 400 на validate до пропагации DNS — не падаем
        try:
            return json.loads(e.read())
        except Exception:
            return {"status": "error", "http": e.code}


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


def validate(domain):
    v1 = uni("domain/validate-verification-record.json", {"domain": domain})
    v2 = uni("domain/validate-dkim.json", {"domain": domain})
    ok1 = v1.get("status") == "success"
    ok2 = v2.get("status") == "success"
    print(f"   verification: {'✓ подтверждена' if ok1 else '⏳ ' + str(v1.get('message', v1))[:70]}")
    print(f"   dkim:         {'✓ подтверждён' if ok2 else '⏳ ' + str(v2.get('message', v2))[:70]}")
    return ok1 and ok2


def main():
    if os.getenv("VALIDATE_ONLY"):
        print(f"== Validate {DOMAIN} ==")
        done = validate(DOMAIN)
        print("Домен готов к отправке ✅" if done else "Ещё не готов — DNS пропагируется, повтори позже.")
        return
    print(f"== Provision {DOMAIN} ==")
    rec = uni("domain/get-dns-records.json", {"domain": DOMAIN})
    vhash, dkim, dmarc = rec.get("verification-record"), rec.get("dkim"), rec.get("dmarc")
    if not dkim:
        print("!! get-dns-records не вернул DKIM:", rec); return
    print(f"   Unisender records ok (dkim {len(dkim)} симв, dmarc {dmarc})")

    rrs = existing_rrs()
    blob = json.dumps(rrs, ensure_ascii=False)
    spf_exists = "v=spf1" in blob
    plan = []
    if vhash and "unisender-go-validate-hash" not in blob:
        plan.append(("zone/add_txt", {"domains": [{"dname": DOMAIN}], "subdomain": "@", "text": vhash}, "verification TXT @"))
    if "DKIM1" not in blob or "us._domainkey" not in blob:
        plan.append(("zone/add_txt", {"domains": [{"dname": DOMAIN}], "subdomain": "us._domainkey",
                     "text": f"v=DKIM1; k=rsa; p={dkim}"}, "DKIM TXT us._domainkey"))
    if "dmarc.unisender" not in blob:
        # CNAME в Reg.ru = zone/add_cname (canonical_name). add_alias — это A-запись (ipaddr)!
        plan.append(("zone/add_cname", {"domains": [{"dname": DOMAIN}], "subdomain": "_dmarc",
                     "canonical_name": dmarc}, "DMARC CNAME _dmarc"))
    if not spf_exists:
        plan.append(("zone/add_txt", {"domains": [{"dname": DOMAIN}], "subdomain": "@",
                     "text": "v=spf1 include:spf.unisender.ru ~all"}, "SPF TXT @"))
    else:
        print("   SPF уже есть — не трогаю (добавь include:spf.unisender.ru в него вручную при желании)")

    for method, data, label in plan:
        r = regru(method, data)
        print(f"   + {label}: {r.get('result', r)}")
    if not plan:
        print("   все нужные записи уже на месте")

    time.sleep(3)
    print("   Проверка DNS (может быть pending до пропагации):")
    if validate(DOMAIN):
        print("Домен подтверждён — можно слать письма ✅")
    else:
        print("Записи добавлены. DNS пропагация — минуты–часы. Повтори валидацию позже:")
        print(f"   VALIDATE_ONLY=1 ./provision.sh {DOMAIN}")


main()
