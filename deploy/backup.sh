#!/usr/bin/env bash
# Nightly pg_dump. Keeps KEEP_DAYS local copies; uploads to GCS if GCS_BUCKET is set
# (e.g. in /opt/tele-sync/.env: GCS_BUCKET=gs://my-telesync-backups).
# Installed by setup.sh as a cron job of the telesync user.
set -euo pipefail

ENV_FILE=/opt/tele-sync/.env
BACKUP_DIR=${BACKUP_DIR:-/var/lib/tele-sync/backups}
KEEP_DAYS=${KEEP_DAYS:-7}

# Read only the two variables we need from .env.
DATABASE_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | cut -d= -f2-)
GCS_BUCKET=$(grep -E '^GCS_BUCKET=' "$ENV_FILE" | cut -d= -f2- || true)

mkdir -p "$BACKUP_DIR"
out="$BACKUP_DIR/telesync-$(date -u +%Y%m%d-%H%M%S).dump"
pg_dump --format=custom --compress=6 --no-owner --dbname="$DATABASE_URL" --file="$out.tmp"
mv "$out.tmp" "$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"

if [[ -n "${GCS_BUCKET:-}" ]]; then
  gcloud storage cp "$out" "${GCS_BUCKET%/}/" --quiet
  echo "uploaded to $GCS_BUCKET"
fi

find "$BACKUP_DIR" -name 'telesync-*.dump' -mtime +"$KEEP_DAYS" -delete
