"""Замер фактической тарификации apipoint. Запускать сразу после пополнения.

Отвечает на вопрос, который не описан ни в документации, ни в оферте и от
которого зависит вся экономика: тарифицируются ли ОПРОСЫ ГОТОВНОСТИ у
асинхронного метода reportjson. Заявленные 50 ₽ — это за отчёт или за каждый из
примерно тридцати вызовов? Разница между 50 ₽ и полутора тысячами за отчёт.

Попутно меряем то, чего тоже нет в документации: тарифицируется ли ПУСТОЙ ответ
(машина есть, данных по блоку нет) и совпадают ли фактические списания с прайсом.

Ничего не ломает: только читает, каждый вызов пишется в журнал модуля.

    python3 probe_billing.py [VIN]
"""
from __future__ import annotations

import json
import sys
import time

import apipoint as ap

VIN = (sys.argv[1] if len(sys.argv) > 1 else "WBA11AD0305R89199").upper()

# Порядок важен: сначала самое дешёвое. Если денег хватит только на часть,
# успеем узнать главное — сходятся ли фактические цены с прайсом.
CHEAP = ["dtp", "zalog", "vindecode", "eaisto", "gost"]


def rub(v) -> str:
    return "—" if v is None else f"{v:.2f}"


def main() -> None:
    bal0 = ap.balance()
    print(f"баланс до начала: {rub(bal0)} ₽")
    if not bal0:
        print("\nБаланс нулевой — пополните на https://apipoint.ru и запустите снова.")
        print("Ста рублей хватит с запасом: полный прогон стоит около 55 ₽.")
        return

    print("\n=== ПОШТУЧНЫЕ МЕТОДЫ: сверяем факт с прайсом")
    print(f"{'метод':22s}{'прайс':>8}{'списано':>10}{'остаток':>10}  результат")
    for m in CHEAP:
        try:
            d = ap.call(m, {"vin": VIN})
        except ap.NoFunds:
            print(f"{m:22s}  ДЕНЬГИ КОНЧИЛИСЬ — остановился"); return
        except ap.ApiPointError as e:
            print(f"{m:22s}  ошибка: {e}"); continue
        res = d.get("result")
        empty = res in (None, {}, [], "")
        price, listed = ap._num(d.get("price")), ap.PRICES.get(m)
        mark = "" if price == listed else "  ← РАСХОЖДЕНИЕ С ПРАЙСОМ"
        print(f"{m:22s}{rub(listed):>8}{rub(price):>10}{rub(ap._num(d.get('balance'))):>10}"
              f"  {'ПУСТО' if empty else 'есть данные'}{mark}")
        if empty and price:
            print(f"{'':22s}  ↑ пустой ответ ТАРИФИЦИРУЕТСЯ ({rub(price)} ₽)")

    print("\n=== ГЛАВНОЕ: тарифицируются ли опросы готовности reportjson")
    try:
        created = ap.call("reportjson", {"mode": "create", "vin": VIN})
    except ap.NoFunds:
        print("  денег не хватило на reportjson (нужно от 50 ₽)"); return
    except ap.ApiPointError as e:
        print(f"  create не прошёл: {e}"); return

    task = (created.get("result") or {}).get("Task") or {}
    tid = task.get("ID") or (created.get("result") or {}).get("ID")
    spent_create = ap._num(created.get("price")) or 0.0
    bal = ap._num(created.get("balance"))
    print(f"  create: списано {rub(spent_create)} ₽, остаток {rub(bal)}, задача {tid}")
    if not tid:
        print("  ID задачи не пришёл, дальше проверить нечем:",
              json.dumps(created, ensure_ascii=False)[:300]); return

    spent_polls, polls = 0.0, 0
    status = None
    # Документация советует опрашивать раз в 10 секунд первые 5 минут.
    for _ in range(30):
        time.sleep(10)
        polls += 1
        try:
            ch = ap.call("reportjson", {"mode": "check", "id": tid}, attempts=1)
        except ap.NoFunds:
            print(f"  ДЕНЬГИ КОНЧИЛИСЬ на {polls}-м опросе — значит опросы ПЛАТНЫЕ"); return
        except ap.ApiPointError as e:
            print(f"  опрос {polls}: ошибка {e}"); continue
        p = ap._num(ch.get("price")) or 0.0
        spent_polls += p
        status = (ch.get("result") or {}).get("Status")
        print(f"  опрос {polls:>2}: статус {status}, списано {rub(p)} ₽, остаток {rub(ap._num(ch.get('balance')))}")
        if str(status) in ("1", "Completed"):
            break

    try:
        got = ap.call("reportjson", {"mode": "result", "id": tid})
        spent_result = ap._num(got.get("price")) or 0.0
        blocks = len(got.get("result") or {})
        print(f"  result: списано {rub(spent_result)} ₽, блоков в отчёте {blocks}")
    except ap.ApiPointError as e:
        spent_result, blocks = 0.0, 0
        print(f"  result не прошёл: {e}")

    total = spent_create + spent_polls + spent_result
    print("\n" + "=" * 70)
    print(f"ИТОГО ОДИН ОТЧЁТ: {rub(total)} ₽")
    print(f"  создание {rub(spent_create)} · опросы {rub(spent_polls)} за {polls} шт · выдача {rub(spent_result)}")
    if spent_polls == 0:
        print("  ВЫВОД: опросы БЕСПЛАТНЫ — отчёт стоит заявленных 50 ₽, модель сходится.")
    else:
        print(f"  ВЫВОД: опросы ПЛАТНЫЕ по {rub(spent_polls / max(polls,1))} ₽ — "
              f"реальная себестоимость {rub(total)} ₽, а не 50 ₽.")
    print(f"\nвсего потрачено за сегодня по журналу: {ap.spent_today()} ₽")


if __name__ == "__main__":
    main()
