#!/bin/bash
# VPS setup script for Pulse (Strava App)
# Tested on Ubuntu 22.04 / Debian 12
# Usage: sudo bash setup.sh

set -e

APP_DIR="/opt/strava-app"
LOG_DIR="/var/log/strava-app"
SERVICE_NAME="strava-app"

echo "==> Updating system packages..."
apt-get update -y && apt-get upgrade -y

echo "==> Installing dependencies..."
apt-get install -y python3 python3-pip python3-venv nginx git

echo "==> Creating app directory..."
mkdir -p "$APP_DIR"
mkdir -p "$LOG_DIR"

echo "==> Copying app files..."
cp -r ../backend "$APP_DIR/backend"
cp -r ../frontend "$APP_DIR/frontend"

echo "==> Creating Python virtual environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install eventlet
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt"

echo "==> Creating data directory..."
mkdir -p "$APP_DIR/backend/data"
chown -R www-data:www-data "$APP_DIR"
chown -R www-data:www-data "$LOG_DIR"

echo "==> Setting up .env (copy from example if not present)..."
if [ ! -f "$APP_DIR/backend/.env" ]; then
    cp "$APP_DIR/backend/.env.example" "$APP_DIR/backend/.env"
    echo ""
    echo "  !! Edit $APP_DIR/backend/.env with your real credentials before starting the service !!"
    echo ""
fi

echo "==> Installing systemd service..."
cp strava-app.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo "==> Installing nginx config..."
cp nginx.conf /etc/nginx/sites-available/strava-app
ln -sf /etc/nginx/sites-available/strava-app /etc/nginx/sites-enabled/strava-app
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo ""
echo "================================================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit $APP_DIR/backend/.env with your Strava credentials"
echo "  2. Start the service:"
echo "       systemctl start $SERVICE_NAME"
echo "  3. Check status:"
echo "       systemctl status $SERVICE_NAME"
echo "       journalctl -u $SERVICE_NAME -f"
echo "================================================================"
