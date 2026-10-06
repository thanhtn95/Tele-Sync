#!/usr/bin/env bash
# One-shot provisioning for a fresh Ubuntu 24.04 e2-micro. Idempotent; re-run to update.
#   sudo bash deploy/setup.sh            (from a clone of this repo)
set -euo pipefail

REPO_SRC=$(cd "$(dirname "$0")/.." && pwd)
APP=/opt/tele-sync
DATA=/var/lib/tele-sync
SWAP_SIZE=${SWAP_SIZE:-2G}

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }

echo "== swap ($SWAP_SIZE)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -q vm.swappiness=10
echo 'vm.swappiness=10' > /etc/sysctl.d/99-telesync.conf

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q postgresql nginx python3-venv python3-pip git curl rsync nodejs npm

echo "== tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "== postgres"
PGVER=$(ls /etc/postgresql | sort -V | tail -1)
install -m 644 "$REPO_SRC/deploy/postgresql-tuning.conf" "/etc/postgresql/$PGVER/main/conf.d/telesync.conf"
systemctl restart postgresql
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='telesync'" | grep -q 1; then
  DB_PASS=$(openssl rand -hex 24)
  sudo -u postgres psql -q -c "CREATE ROLE telesync LOGIN PASSWORD '$DB_PASS'"
  sudo -u postgres psql -q -c "CREATE DATABASE telesync OWNER telesync"
  NEW_DB_URL="postgresql://telesync:$DB_PASS@127.0.0.1:5432/telesync"
fi

echo "== app user + dirs"
id telesync >/dev/null 2>&1 || useradd --system --home-dir "$DATA/home" --create-home --shell /usr/sbin/nologin telesync
mkdir -p "$DATA/files" "$DATA/tmp" "$DATA/backups" "$DATA/home"
chown telesync:www-data "$DATA" "$DATA/files"
chmod 0710 "$DATA"           # nginx may traverse, not list
chmod 2750 "$DATA/files"     # setgid: new files stay group www-data
chown -R telesync:telesync "$DATA/tmp" "$DATA/backups" "$DATA/home"
chmod 700 "$DATA/tmp" "$DATA/backups"

echo "== code -> $APP"
mkdir -p "$APP"
rsync -a --delete --exclude .git --exclude node_modules --exclude 'frontend/dist' --exclude .env \
  --exclude venv "$REPO_SRC/" "$APP/"
chown -R root:root "$APP"

if [[ ! -f "$APP/.env" ]]; then
  cp "$APP/.env.example" "$APP/.env"
  sed -i "s#^TG_SESSION_PATH=.*#TG_SESSION_PATH=$DATA/telegram.session#; \
          s#^MEDIA_DIR=.*#MEDIA_DIR=$DATA/files#; \
          s#^TEMP_DIR=.*#TEMP_DIR=$DATA/tmp#" "$APP/.env"
  echo "GCS_BUCKET=" >> "$APP/.env"
fi
if [[ -n "${NEW_DB_URL:-}" ]]; then
  sed -i "s#^DATABASE_URL=.*#DATABASE_URL=$NEW_DB_URL#" "$APP/.env"
fi
chown root:telesync "$APP/.env"
chmod 640 "$APP/.env"

echo "== python venv"
[[ -d "$APP/venv" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/backend/requirements.txt"

echo "== frontend build"
if [[ -f "$REPO_SRC/frontend/dist/index.html" ]]; then
  rsync -a --delete "$REPO_SRC/frontend/dist/" "$APP/frontend/dist/"   # prebuilt locally
else
  (cd "$APP/frontend" && npm ci --no-audit --no-fund && npm run build && rm -rf node_modules)
fi

echo "== systemd + nginx"
install -m 644 "$APP/deploy/telesync.service" /etc/systemd/system/telesync.service
install -m 644 "$APP/deploy/nginx.conf" /etc/nginx/sites-available/telesync
ln -sf /etc/nginx/sites-available/telesync /etc/nginx/sites-enabled/telesync
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now nginx
systemctl reload nginx
systemctl enable telesync
systemctl restart telesync

echo "== nightly backup (03:17 UTC)"
install -m 755 "$APP/deploy/backup.sh" /usr/local/bin/telesync-backup
echo "17 3 * * * telesync /usr/local/bin/telesync-backup >> $DATA/backups/backup.log 2>&1" > /etc/cron.d/telesync-backup

cat <<MSG

Done. Remaining manual steps:
  1. Edit $APP/.env: TG_API_ID, TG_API_HASH, Google OAuth values, GCS_BUCKET.
  2. Telegram login (creates the session file, interactive):
       cd $APP/backend && sudo -u telesync $APP/venv/bin/python -m scripts.tg_login
       sudo systemctl restart telesync
  3. Google refresh token (see README), then: sudo systemctl restart telesync
  4. Tailscale:  sudo tailscale up --ssh
                 sudo tailscale serve --bg 8080
     -> open https://<vm-name>.<tailnet>.ts.net from any device on your tailnet.
MSG
