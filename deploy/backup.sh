#!/usr/bin/env bash
# ZehnMS backup for the Docker deployment: a PostgreSQL dump plus an archive of uploaded files,
# written to /srv/zehnms/backups and kept for KEEP_DAYS days.
#
# Runs before every deploy (vps-deploy.sh) and nightly from the deploy user's crontab:
#   30 2 * * * bash /srv/zehnms/app/deploy/backup.sh nightly >> /srv/zehnms/backups/backup.log 2>&1
# Restore: docs/DEPLOYMENT.md, "Restoring a backup".
#
# These files sit on the same disk as the database. Copy /srv/zehnms/backups off the server too.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZEHNMS_HOME="${ZEHNMS_HOME:-$(dirname "$APP_DIR")}"
BACKUP_ROOT="${BACKUP_ROOT:-$ZEHNMS_HOME/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
LABEL="${1:-manual}"
export ZEHNMS_HOME

cd "$APP_DIR"
if [[ -z "$(docker compose ps --status running --quiet db 2>/dev/null)" ]]; then
    echo "backup: the db container is not running; nothing to back up" >&2
    exit 1
fi

umask 077
mkdir -p "$BACKUP_ROOT"
stamp="$(date +%Y%m%d-%H%M%S)"

db_file="$BACKUP_ROOT/zehnms-db-$stamp-$LABEL.dump"
docker compose exec -T db pg_dump -U zehnms -d zehnms --format=custom --no-owner --no-privileges > "$db_file.partial"
mv "$db_file.partial" "$db_file"
echo "backup: database -> $db_file ($(du -h "$db_file" | cut -f1))"

if [[ -n "$(docker compose ps --status running --quiet web 2>/dev/null)" ]]; then
    media_file="$BACKUP_ROOT/zehnms-media-$stamp-$LABEL.tar.gz"
    docker compose exec -T web tar -C /data -czf - media > "$media_file.partial"
    mv "$media_file.partial" "$media_file"
    echo "backup: media    -> $media_file ($(du -h "$media_file" | cut -f1))"
fi

find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'zehnms-*' ! -name '*.partial' -mtime +"$KEEP_DAYS" -delete
find "$BACKUP_ROOT" -maxdepth 1 -type f -name '*.partial' -mmin +60 -delete
