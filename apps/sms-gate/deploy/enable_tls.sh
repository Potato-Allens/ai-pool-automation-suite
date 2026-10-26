#!/usr/bin/env bash
# Issue a Let's Encrypt certificate for your-domain.com and enable HTTPS.
set -euo pipefail
DOMAIN="${1:-your-domain.com}"
ROOT=/opt/smsgate

echo "== 1. DNS check =="
IPS=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u || true)
echo "resolved: ${IPS:-none}"
if ! echo "$IPS" | grep -q '^45\.207\.221\.161$'; then
  echo "!! $DOMAIN 还没有解析到 <SERVER_IP>，先到 DNS 服务商添加 A 记录再执行本脚本。"
  exit 1
fi

echo "== 2. enable HTTP vhost =="
install -m 0644 "$ROOT/deploy/nginx-smsgate-domain.conf" /etc/nginx/sites-available/smsgate-domain
ln -sfn /etc/nginx/sites-available/smsgate-domain /etc/nginx/sites-enabled/smsgate-domain
nginx -t
systemctl reload nginx

echo "== 3. certbot =="
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
  -m "admin@$DOMAIN" --redirect --keep-until-expiring

echo "== 4. verify =="
nginx -t
systemctl reload nginx
curl -sS -o /dev/null -w "https status: %{http_code}\n" "https://$DOMAIN/api/health" || true
echo "OK: https://$DOMAIN"