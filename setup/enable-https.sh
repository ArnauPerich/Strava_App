#!/bin/bash
# Enable HTTPS for Pulse using sslip.io + Let's Encrypt (free, valid cert, no domain needed).
#
# sslip.io turns your bare IP into a real hostname automatically:
#   203.0.113.45  ->  203-0-113-45.sslip.io
# Let's Encrypt happily issues a real certificate for that hostname, so the
# browser shows a normal padlock (microphone / getUserMedia works).
#
# Usage:  sudo bash enable-https.sh tu-email@ejemplo.com
set -e

EMAIL="${1:-}"
if [ -z "$EMAIL" ]; then
    echo "!! Falta el email. Uso: sudo bash enable-https.sh tu-email@ejemplo.com"
    echo "   (Let's Encrypt lo pide para avisos de caducidad; no se hace público.)"
    exit 1
fi

NGINX_CONF="/etc/nginx/sites-available/strava-app"
ENV_FILE="/opt/strava-app/backend/.env"

echo "==> Detectando IP pública..."
IP="$(curl -s https://api.ipify.org)"
if [ -z "$IP" ]; then
    echo "!! No se pudo detectar la IP pública automáticamente."
    exit 1
fi
HOST="${IP//./-}.sslip.io"
echo "    IP:     $IP"
echo "    Dominio: $HOST"

echo "==> Instalando certbot..."
apt-get update -y
apt-get install -y certbot python3-certbot-nginx

echo "==> Ajustando server_name en nginx ($HOST)..."
# Replace the catch-all server_name with our sslip.io host so certbot can match it
sed -i "s/server_name .*/server_name $HOST;/" "$NGINX_CONF"
nginx -t && systemctl reload nginx

echo "==> Solicitando certificado a Let's Encrypt..."
# --nginx adds the 443 block + cert lines, --redirect forces HTTP -> HTTPS
certbot --nginx -d "$HOST" \
    --non-interactive --agree-tos --redirect \
    -m "$EMAIL"

echo "==> Actualizando REDIRECT_URI en .env a HTTPS..."
if [ -f "$ENV_FILE" ]; then
    if grep -q '^REDIRECT_URI=' "$ENV_FILE"; then
        sed -i "s#^REDIRECT_URI=.*#REDIRECT_URI=https://$HOST/callback#" "$ENV_FILE"
    else
        echo "REDIRECT_URI=https://$HOST/callback" >> "$ENV_FILE"
    fi
    systemctl restart strava-app || true
fi

echo ""
echo "================================================================"
echo "  HTTPS activado en:  https://$HOST"
echo ""
echo "  ULTIMO PASO MANUAL (en Strava):"
echo "  Ve a https://www.strava.com/settings/api y pon"
echo "  'Authorization Callback Domain':"
echo ""
echo "      $HOST"
echo ""
echo "  (sin https:// ni /callback, solo el dominio)"
echo ""
echo "  La renovacion del certificado es automatica (certbot.timer)."
echo "================================================================"
