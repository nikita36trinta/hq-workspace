#!/usr/bin/env bash
# Проверочное письмо на $TEST_EMAIL (из общего hq-env).
#   ./test.sh [from_email]      from_email по умолчанию noreply@chistasdelka.ru
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$(cd "$DIR/../.hq/pipelines" && pwd)/.env"
get() { grep "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
FROM="${1:-noreply@chistasdelka.ru}"
TO="$(get TEST_EMAIL)"
[ -n "$TO" ] || { echo "нет TEST_EMAIL в $ENV_FILE"; exit 1; }
UNISENDER_GO_API_KEY="$(get UNISENDER_GO_API_KEY)" python3 "$DIR/mailer.py" \
  "$TO" "Тест доставки — mailer ✅" \
  "<div style='font-family:sans-serif;font-size:15px;line-height:1.5'><p>Проверка доставки транзакционных писем через Unisender Go.</p><p>Если это письмо в инбоксе — mailer работает и домен настроен верно ✅</p></div>" \
  "$FROM" "NutriPlan / ЧистаяСделка"
