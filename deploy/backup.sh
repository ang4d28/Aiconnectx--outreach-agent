#!/usr/bin/env bash
# Dumps the database to <project>/backups/aiconnectx-<timestamp>.sql.gz and keeps the newest 7 files.
# Run nightly by aiconnectx-backup.timer; can also be run by hand.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
KEEP=7

env_get() {
    local value
    value="$(grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    printf '%s' "$value"
}

[ -f "$ENV_FILE" ] || { echo "backup: $ENV_FILE not found" >&2; exit 1; }

DB_HOST="$(env_get POSTGRES_HOST)"
DB_PORT="$(env_get POSTGRES_PORT)"
DB_NAME="$(env_get POSTGRES_DB)"
DB_USER="$(env_get POSTGRES_USER)"
PGPASSWORD="$(env_get POSTGRES_PASSWORD)"
export PGPASSWORD

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

target="$BACKUP_DIR/aiconnectx-$(date +%Y%m%d-%H%M%S).sql.gz"
pg_dump -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" -U "$DB_USER" "$DB_NAME" | gzip > "$target.tmp"
mv "$target.tmp" "$target"
echo "backup: wrote $target"

mapfile -t old < <(find "$BACKUP_DIR" -maxdepth 1 -name 'aiconnectx-*.sql.gz' -printf '%T@ %p\n' | sort -rn | tail -n +$((KEEP + 1)) | cut -d' ' -f2-)
for file in "${old[@]}"; do
    rm -f -- "$file"
    echo "backup: removed $file"
done
