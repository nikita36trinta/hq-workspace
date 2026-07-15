#!/usr/bin/env bash
# Однокомандный провижн ПОЛНОГО email-сервиса для домена: ОТПРАВКА + ПРИЁМ.
#   ./email_service.sh <domain> [server_ip]
#
# Делает end-to-end и идемпотентно:
#   1. Sending-домен: регистрация в Unisender + DKIM/SPF/DMARC/verification (Reg.ru API).
#   2. Inbound DNS: A mail/inbox + MX (Reg.ru API).
#   3. Приёмник: разворачивает (или дополняет) самописный SMTP :25 + мини-админку на server_ip,
#      добавляет домен в whitelist приёмника (мультидоменный, один :25 на все домены).
#
# Разовые предпосылки (НЕ автоматизируются — уровень аккаунта, см. память reference-mailer-pipeline):
#   - Unisender Go аккаунт + tracking-домен, назначенный дефолтным backend (иначе отправка = error 229).
#   - Reg.ru: IP whitelist-хоста добавлен в «Настройки API → Диапазоны IP».
set -euo pipefail
DOMAIN="${1:?usage: email_service.sh <domain> [server_ip]}"
SERVER_IP="${2:-141.105.68.17}"                       # где крутится приёмник (/opt/mailin)
WL_HOST="${REGRU_WHITELIST_HOST:-141.105.68.17}"      # Reg.ru API whitelisted host
DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$(cd "$DIR/../.hq/pipelines" && pwd)/.env"
get(){ grep "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
RE="$(get REGRU_EMAIL)"; RP="$(get REGRU_PASSWORD)"; UK="$(get UNISENDER_GO_API_KEY)"
[ -n "$RE" ] && [ -n "$RP" ] && [ -n "$UK" ] || { echo "нет кредов в $ENV_FILE"; exit 1; }

echo "===================================================================="
echo " EMAIL-СЕРВИС для $DOMAIN   (приёмник на $SERVER_IP)"
echo "===================================================================="

echo; echo "== 1/3 Sending-домен (DKIM/SPF/DMARC/verification) =="
"$DIR/provision.sh" "$DOMAIN" || echo "  (provision вернул ненулевой код — см. вывод выше)"

echo; echo "== 2/3 Inbound DNS (A mail/inbox, MX) =="
ssh -o ConnectTimeout=20 "root@$WL_HOST" \
  "REGRU_EMAIL='$RE' REGRU_PASSWORD='$RP' DOMAIN='$DOMAIN' SERVER_IP='$SERVER_IP' python3 -" \
  < "$DIR/inbound/provision_inbound_dns.py"

echo; echo "== 3/3 Приёмник входящих на $SERVER_IP =="
tar czf - -C "$DIR/inbound" inbound_smtp.py inbox_admin.py Dockerfile docker-compose.yml provision_inbound_dns.py \
  | ssh -o ConnectTimeout=20 "root@$SERVER_IP" 'mkdir -p /opt/mailin && tar xzf - -C /opt/mailin'
ssh -o ConnectTimeout=90 "root@$SERVER_IP" 'bash -s' "$DOMAIN" <<'REMOTE'
set -euo pipefail
DOMAIN="$1"; cd /opt/mailin; touch .env
# creds админки — сгенерировать при первом запуске
grep -q '^INBOX_PASS=' .env || {
  P=$(openssl rand -base64 15 | tr -d '/+=' | cut -c1-18)
  printf 'INBOX_USER=admin\nINBOX_PASS=%s\n' "$P" >> .env
}
# хост админки — первый домен задаёт inbox.<domain> (центральная админка на все домены)
grep -q '^INBOX_HOST=' .env || echo "INBOX_HOST=inbox.$DOMAIN" >> .env
# MAILIN_DOMAINS — добавить домен идемпотентно
CUR=$(grep '^MAILIN_DOMAINS=' .env | cut -d= -f2- || true)
case ",$CUR," in *",$DOMAIN,"*) NEW="$CUR";; "" ) NEW="$DOMAIN";; ,, ) NEW="$DOMAIN";; *) NEW="${CUR:+$CUR,}$DOMAIN";; esac
if grep -q '^MAILIN_DOMAINS=' .env; then sed -i "s|^MAILIN_DOMAINS=.*|MAILIN_DOMAINS=$NEW|" .env; else echo "MAILIN_DOMAINS=$NEW" >> .env; fi
chmod 600 .env
ufw allow 25/tcp comment 'smtp-inbound' >/dev/null 2>&1 || true
docker compose up -d --build 2>&1 | tail -4
echo "домены приёмника: $NEW"
echo "--- админка ---"; grep -E '^INBOX_(USER|PASS|HOST)=' .env
REMOTE

echo; echo "== ГОТОВО =="
echo "Отправка:  send_email(from_email=noreply@$DOMAIN, reply_to=support@$DOMAIN, ...)"
echo "Приём:     hello/support/noreply/info@$DOMAIN  → централь. админка (INBOX_HOST выше)"
echo "Разово/аккаунт: tracking-домен по умолчанию в Unisender Go (см. reference-mailer-pipeline)."
echo "После DNS-пропагации: свежий tracking у Unisender активируется сам; входящие проверь письмом на support@$DOMAIN."
