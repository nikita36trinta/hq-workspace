#!/usr/bin/env bash
# Провижн домена-отправителя одной командой:
#   ./provision.sh <domain>
# Гоняет provision_core.py НА Reg.ru-whitelisted хосте (там разрешён IP для Reg.ru API),
# креды берёт из общего hq-env (.hq/pipelines/.env).
set -euo pipefail
DOMAIN="${1:?usage: provision.sh <domain>}"
HOST="${REGRU_WHITELIST_HOST:-141.105.68.17}"
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$(cd "$DIR/../.hq/pipelines" && pwd)/.env"

get() { grep "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
UK="$(get UNISENDER_GO_API_KEY)"; RE="$(get REGRU_EMAIL)"; RP="$(get REGRU_PASSWORD)"
[ -n "$UK" ] && [ -n "$RE" ] && [ -n "$RP" ] || { echo "нет кредов в $ENV_FILE"; exit 1; }

ssh -o ConnectTimeout=15 "root@$HOST" \
  "VALIDATE_ONLY='${VALIDATE_ONLY:-}' UNISENDER_GO_API_KEY='$UK' REGRU_EMAIL='$RE' REGRU_PASSWORD='$RP' DOMAIN='$DOMAIN' python3 -" \
  < "$DIR/provision_core.py"
