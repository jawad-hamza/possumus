#!/usr/bin/env bash
# Build and (re)start ZehnMS on the VPS with Docker Compose. Run as the deploy user (a member of
# the docker group, no sudo needed) from the code folder:
#
#   First time:  bash /srv/zehnms/app/deploy/vps-deploy.sh --domain shop.example.com
#   Updates:     bash /srv/zehnms/app/deploy/vps-deploy.sh
#
# deploy/upload.ps1 uploads the code from Windows and runs this script for you.
#
# Steps: create /srv/zehnms/.env and db.env on the first run (random secret key and database
# password) -> back up the running database -> build the image -> start the containers (the app
# migrates on start) -> wait until healthy. nginx and the certificate are a one-time manual step
# (printed at the end until the site exists).
set -euo pipefail

# Braces: bash reads the whole script first, so an upload replacing this file mid-run is harmless.
{
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ZEHNMS_HOME="${ZEHNMS_HOME:-$(dirname "$APP_DIR")}"
export ZEHNMS_HOME
ENV_FILE="$ZEHNMS_HOME/.env"
DB_ENV_FILE="$ZEHNMS_HOME/db.env"
DOMAIN=""
SKIP_BACKUP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="${2:-}"; shift 2 ;;
        --skip-backup) SKIP_BACKUP=1; shift ;;
        -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mError: %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
docker info >/dev/null 2>&1 || die "cannot talk to Docker; is $(whoami) in the docker group?"
cd "$APP_DIR"
[[ -f compose.yaml ]] || die "compose.yaml not found in $APP_DIR"

umask 077
mkdir -p "$ZEHNMS_HOME/backups"

if [[ ! -f "$ENV_FILE" ]]; then
    step "First run: writing $ENV_FILE and $DB_ENV_FILE"
    [[ -n "$DOMAIN" ]] || die "first run needs --domain shop.example.com"
    [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || die "--domain must be a bare host name such as shop.example.com"
    [[ ! -f "$DB_ENV_FILE" ]] || die "$DB_ENV_FILE exists without $ENV_FILE; restore the .env or remove both"
    secret_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
    db_password="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
    sed -e "s|__DOMAIN__|$DOMAIN|g" \
        -e "s|__SECRET_KEY__|$secret_key|g" \
        -e "s|__DB_PASSWORD__|$db_password|g" \
        deploy/env.vps.example > "$ENV_FILE"
    printf 'POSTGRES_PASSWORD=%s\n' "$db_password" > "$DB_ENV_FILE"
    chmod 600 "$ENV_FILE" "$DB_ENV_FILE"
    echo "Created. Fill in the DJANGO_EMAIL_* settings in $ENV_FILE before owners sign up."
elif [[ -n "$DOMAIN" ]]; then
    echo "Note: $ENV_FILE already exists; --domain is ignored (edit the file to change it)."
fi

if [[ $SKIP_BACKUP -eq 0 && -n "$(docker compose ps --status running --quiet db 2>/dev/null)" ]]; then
    step "Backing up before the update"
    bash deploy/backup.sh pre-deploy
fi

step "Building the image"
# Keep the running image as zehnms:previous for a quick rollback (DEPLOYMENT.md, "Rolling back").
if docker image inspect zehnms:latest >/dev/null 2>&1; then
    docker tag zehnms:latest zehnms:previous
fi
docker compose build --pull web

step "Starting the containers"
docker compose up -d --remove-orphans

step "Waiting for the app to become healthy"
web_id="$(docker compose ps --quiet web)"
status=""
for _ in $(seq 1 60); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$web_id" 2>/dev/null || echo missing)"
    [[ "$status" == "healthy" ]] && break
    sleep 2
done
if [[ "$status" != "healthy" ]]; then
    docker compose logs --tail 80 web || true
    die "the app is '$status'. Logs above; the pre-deploy dump is in $ZEHNMS_HOME/backups."
fi
port="$(docker compose port web 8000 | cut -d: -f2)"
echo "Healthy on 127.0.0.1:$port"

# Untagged leftovers of earlier ZehnMS builds only; other apps' images are never touched.
docker image prune -f --filter "label=com.docker.compose.project=zehnms" >/dev/null 2>&1 || true

if ! crontab -l 2>/dev/null | grep -q "deploy/backup.sh"; then
    cat <<EOF

Nightly backups are not scheduled yet. Add them with:
  (crontab -l 2>/dev/null; echo '30 2 * * * bash $APP_DIR/deploy/backup.sh nightly >> $ZEHNMS_HOME/backups/backup.log 2>&1') | crontab -
EOF
fi

if [[ ! -e /etc/nginx/sites-enabled/zehnms ]]; then
    host="$(grep -E '^DJANGO_ALLOWED_HOSTS=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- | cut -d, -f1 | tr -d "\"' ")"
    cat <<EOF

One-time nginx + https setup (needs sudo; DNS for $host must point at this server):
  sudo certbot certonly --nginx -d $host
  sed "s|__DOMAIN__|$host|g" $APP_DIR/deploy/nginx/zehnms.conf | sudo tee /etc/nginx/sites-available/zehnms >/dev/null
  sudo ln -s /etc/nginx/sites-available/zehnms /etc/nginx/sites-enabled/zehnms
  sudo nginx -t && sudo systemctl reload nginx
  curl -s https://$host/healthz/
EOF
fi
exit 0
}
