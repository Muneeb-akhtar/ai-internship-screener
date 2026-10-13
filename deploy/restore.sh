#!/usr/bin/env bash
# Restore database dumps into the running stack (used to MOVE your local data to the
# server, or to recover from a backup).
#   bash deploy/restore.sh backups/n8n_local.dump backups/screener_local.dump
#
# The n8n credentials (Google Sheets/Gmail) inside the n8n dump are encrypted with
# N8N_ENCRYPTION_KEY - the server's .env must use the SAME key as where the dump came from.
set -euo pipefail

N8N_DUMP=${1:?usage: restore.sh <n8n.dump> <screener.dump>}
SCREENER_DUMP=${2:?usage: restore.sh <n8n.dump> <screener.dump>}
cd /opt/screener
# Read only the values we need (sourcing .env breaks on values with spaces)
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '\r'; }
POSTGRES_USER=$(env_get POSTGRES_USER)
N8N_DB_NAME=$(env_get N8N_DB_NAME); N8N_DB_USER=$(env_get N8N_DB_USER)
SCREENER_DB_NAME=$(env_get SCREENER_DB_NAME); SCREENER_DB_USER=$(env_get SCREENER_DB_USER)
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

echo "==> Stopping the apps (Postgres keeps running)"
"${COMPOSE[@]}" stop n8n screener-api caddy || true

restore() {  # restore <db> <owner> <file>
  echo "==> Restoring $1 from $3"
  "${COMPOSE[@]}" exec -T postgres pg_restore -U "${POSTGRES_USER:-postgres}" -d "$1" \
    --clean --if-exists --no-owner --role="$2" < "$3"
}
restore "${N8N_DB_NAME:-n8n}" "${N8N_DB_USER:-n8n}" "$N8N_DUMP"
restore "${SCREENER_DB_NAME:-screener}" "${SCREENER_DB_USER:-screener}" "$SCREENER_DUMP"

echo "==> Starting everything again"
"${COMPOSE[@]}" --profile prod up -d
"${COMPOSE[@]}" ps
