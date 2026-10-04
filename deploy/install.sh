#!/usr/bin/env bash
# Installs AI ConnectX natively on Ubuntu 22.04 (no containers).
#
#   sudo DOMAIN=agent.example.com ./deploy/install.sh
#
# Run it from a clone of the repository with a filled-in .env in the project root.
# Safe to run again: it updates packages, the database password, the build and the services.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo DOMAIN=... $0" >&2
    exit 1
fi

DOMAIN="${DOMAIN:-${1:-}}"
APP_USER="${APP_USER:-aiconnectx}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
WEB_ROOT=/var/www/aiconnectx
NODE_MAJOR=20

[ -n "$DOMAIN" ] || { echo "Set DOMAIN, e.g. sudo DOMAIN=agent.example.com $0" >&2; exit 1; }
[ -f "$ENV_FILE" ] || { echo "$ENV_FILE not found. Copy .env.example to .env and fill it in first." >&2; exit 1; }

env_get() {
    local value
    value="$(grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    printf '%s' "$value"
}

env_set() {
    local tmp
    tmp="$(mktemp)"
    grep -vE "^$1=" "$ENV_FILE" > "$tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
}

render() {
    sed -e "s|__APP_DIR__|$ROOT|g" -e "s|__APP_USER__|$APP_USER|g" "$1" > "$2"
}

DB_NAME="$(env_get POSTGRES_DB)"
DB_USER="$(env_get POSTGRES_USER)"
DB_PASSWORD="$(env_get POSTGRES_PASSWORD)"
DB_NAME="${DB_NAME:-aiconnectx}"
DB_USER="${DB_USER:-aiconnectx}"

[ -n "$DB_PASSWORD" ] || { echo "POSTGRES_PASSWORD is empty in .env" >&2; exit 1; }
[ "$DB_USER" != "postgres" ] || { echo "Set POSTGRES_USER in .env to a dedicated user (not postgres)" >&2; exit 1; }
[ "$(env_get JWT_SECRET | wc -c)" -ge 16 ] || { echo "JWT_SECRET in .env must be 16+ characters (openssl rand -hex 32)" >&2; exit 1; }
[[ "$DB_NAME" =~ ^[a-z_][a-z0-9_]*$ && "$DB_USER" =~ ^[a-z_][a-z0-9_]*$ ]] || { echo "POSTGRES_DB and POSTGRES_USER must use lowercase letters, digits and underscores" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive

echo "==> Packages"
apt-get update
apt-get install -y ca-certificates curl gnupg git ufw software-properties-common debian-keyring debian-archive-keyring apt-transport-https postgresql postgresql-contrib

if ! command -v python3.11 >/dev/null 2>&1; then
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
fi
apt-get install -y python3.11 python3.11-venv python3.11-dev

if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt "$NODE_MAJOR" ]; then
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
    apt-get install -y nodejs
fi

if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
fi

echo "==> Service user $APP_USER"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> Production settings in .env"
sed -i 's/[[:cntrl:]]$//' "$ENV_FILE"  # strip Windows line endings
env_set APP_ENV production
env_set BACKEND_URL "https://$DOMAIN"
env_set FRONTEND_URL "https://$DOMAIN"
env_set POSTGRES_HOST localhost
env_set BACKEND_RELOAD false
chown -R "$APP_USER:$APP_USER" "$ROOT"
chmod 600 "$ENV_FILE"

echo "==> Database $DB_NAME (user $DB_USER)"
systemctl enable --now postgresql
(
    cd /
    export DB_PASSWORD
    sudo --preserve-env=DB_PASSWORD -u postgres psql -v ON_ERROR_STOP=1 -v u="$DB_USER" -v d="$DB_NAME" <<'SQL'
\set pw `printenv DB_PASSWORD`
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'u', :'pw') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'u') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'u', :'pw') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'d', :'u') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'d') \gexec
SQL
)

echo "==> Backend (venv, dependencies, migrations)"
sudo -u "$APP_USER" bash -c "
    set -euo pipefail
    cd '$ROOT/backend'
    python3.11 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e .
    .venv/bin/playwright install chromium
    .venv/bin/alembic -c app/db/alembic.ini upgrade head
"
"$ROOT/backend/.venv/bin/playwright" install-deps chromium

echo "==> Frontend build"
sudo -u "$APP_USER" bash -c "
    set -euo pipefail
    cd '$ROOT/frontend'
    npm ci
    npm run build
"
mkdir -p "$WEB_ROOT"
rm -rf "${WEB_ROOT:?}"/*
cp -r "$ROOT/frontend/dist/." "$WEB_ROOT/"
chmod -R a+rX "$WEB_ROOT"

echo "==> Caddy for $DOMAIN"
install -m 644 "$ROOT/deploy/Caddyfile" /etc/caddy/Caddyfile
printf 'DOMAIN=%s\n' "$DOMAIN" > /etc/caddy/caddy.env
chmod 644 /etc/caddy/caddy.env
mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/caddy/caddy.env\n' > /etc/systemd/system/caddy.service.d/domain.conf

echo "==> systemd units"
for unit in aiconnectx-backend.service aiconnectx-scheduler.service aiconnectx-backup.service; do
    render "$ROOT/deploy/$unit" "/etc/systemd/system/$unit"
done
install -m 644 "$ROOT/deploy/aiconnectx-backup.timer" /etc/systemd/system/aiconnectx-backup.timer
chmod +x "$ROOT/deploy/backup.sh" "$ROOT/deploy/install.sh"
systemctl daemon-reload
systemctl enable aiconnectx-backend.service aiconnectx-scheduler.service aiconnectx-backup.timer caddy
systemctl restart aiconnectx-backend.service aiconnectx-scheduler.service
systemctl restart caddy
systemctl start aiconnectx-backup.timer

echo "==> Firewall (only 22, 80, 443)"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable
ufw status verbose

cat <<DONE

Installed. Next:
  1. Create the first admin (from $ROOT/backend):
       sudo -u $APP_USER $ROOT/backend/.venv/bin/python -m app.cli create-admin --email you@example.com
  2. Open https://$DOMAIN/ (the certificate can take a minute on the first request).
  3. Follow deploy/README-DEPLOY.md for the Microsoft redirect URI, the Meta webhook and the one-time logins.
DONE
