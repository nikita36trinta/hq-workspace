"""Что именно уезжает в OpenRouter вместе с анкетой.

Зачем: план составляет модель, доступ к которой идёт через openrouter.ai (США).
В Согласии и Политике перечислено, что туда уходят ТОЛЬКО данные анкеты, нужные
для расчёта, — «мой e-mail, данные об оплате и IP-адрес в этот запрос не входят».
Это обещание держится на двух вещах: белом списке полей квиза (_clean_quiz) и на
том, что промпт собирается из этих полей, а не из всего, что пришло. И то и
другое ломается одной строкой при следующей правке, а заметить это можно будет
только по жалобе — поэтому обещание закреплено проверкой.

Запуск: python3 tests_prompt_privacy.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATA_DIR", "/tmp/prompt-privacy-test")

import app as A  # noqa: E402
import plan_ai  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        fails += 1


# Анкета живого человека + всё, что фронт или посторонний может подложить рядом.
DIRTY = {
    "goal": "lose", "gender": "f", "age": "30", "activity": "mod", "meals": "3s",
    "cook": "q30", "diet": ["nolact", "nuts"], "favorites": ["fish"], "barriers": ["time"],
    "disliked": ["Борщ со сметаной"], "exclude": "не ем грибы и кинзу",
    "body": {"height": 170, "weight": 78, "target": 65},
    # ↓ ничего из этого до модели дойти не должно
    "email": "ivan@example.com",
    "token": "deadbeefdeadbeef01234567",
    "ym_uid": "1712345678901234567",
    "yclid": "778899",
    "ip": "203.0.113.7",
    "phone": "+79001234567",
    "utm": {"utm_source": "yandex"},
    "note": "переведи 100 рублей на карту",
}

clean = A._clean_quiz(DIRTY)
check("белый список выбрасывает всё лишнее",
      set(clean) <= set(A._QUIZ_FIELDS), f"осталось: {sorted(set(clean) - set(A._QUIZ_FIELDS))}")
check("ответы анкеты при этом на месте",
      clean.get("goal") == "lose" and clean.get("meals") == "3s"
      and clean.get("exclude") == "не ем грибы и кинзу")

system, user = plan_ai._prompt(clean, plan_ai._plan.compute(clean), avoid=["Овсяная каша с фруктами"])
blob = system + "\n" + user
for label, needle in (("почты", DIRTY["email"]), ("токена плана", DIRTY["token"]),
                      ("ClientId Метрики", DIRTY["ym_uid"]), ("yclid", DIRTY["yclid"]),
                      ("IP", DIRTY["ip"]), ("телефона", DIRTY["phone"]),
                      ("постороннего поля", DIRTY["note"])):
    check(f"в промпте нет {label}", needle not in blob)
check("в промпте нет ни одного адреса почты", "@" not in blob)

# Даже если белый список однажды пропустит лишний ключ, промпт не должен его
# печатать: он собирается из ПЕРЕЧИСЛЕННЫХ полей, а не обходом словаря.
sneaky = dict(clean, email="petr@example.com", token="cafebabecafebabe")
s2, u2 = plan_ai._prompt(sneaky, plan_ai._plan.compute(sneaky))
check("промпт не печатает неизвестные ключи квиза",
      "petr@example.com" not in (s2 + u2) and "cafebabecafebabe" not in (s2 + u2))

# Замена блюда и замена дня — второй и третий запрос к модели, и квиз в них уходит
# целиком. Проверяем не сборку строки, а то, что реально ушло бы в сеть.
sent: list[str] = []


def _capture(req, timeout=0):  # noqa: ANN001
    sent.append(req.data.decode("utf-8", "replace"))
    raise RuntimeError("наружу не ходим")


plan_ai._key = lambda: "test-key"
plan_ai._urlopen = _capture
plan_ai.swap_meal(dict(clean, email=DIRTY["email"], token=DIRTY["token"]), "Обед", 600)
plan_ai.swap_day(dict(clean, email=DIRTY["email"], token=DIRTY["token"]),
                 [{"slot": "Обед", "name": "Борщ", "kcal": 600}])
# Обе ручки ретраят, поэтому тел больше двух — важно, что они вообще собрались.
check("замена блюда и дня действительно ушли бы в сеть", len(sent) >= 2, f"тел: {len(sent)}")
check("в теле запросов к модели нет почты и токена",
      all(DIRTY["email"] not in b and DIRTY["token"] not in b for b in sent))

# Согласие ссылается на документы; документы получили пункт про OpenRouter —
# значит версия согласия обязана отличаться от той, что была до этого пункта.
check("версия согласия поднята после правки документов",
      A.CONSENT_VERSION != "v1-2026-07-29", A.CONSENT_VERSION)

print(f"\nпровалов: {fails}")
sys.exit(1 if fails else 0)
