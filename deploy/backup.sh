#!/usr/bin/env bash
# Nightly Postgres backup -> S3. Installed as a cron job by deploy/README.md:
#   0 2 * * * /opt/screener/deploy/backup.sh >> /opt/screener/backups/backup.log 2>&1
#
# Dumps both databases (n8n + screener) with pg_dump (custom format, compressed),
# keeps 7 local copies, uploads to s3://$BACKUP_S3_BUCKET/postgres/.
# Credentials: the EC2 instance's IAM role (no AWS keys stored on the server).
# Old S3 copies are removed by the bucket's lifecycle rule (see README).
set -euo pipefail

APP_DIR=/opt/screener
cd "$APP_DIR"
# Read only the values we need. (Don't `source .env`: values with spaces, like
# COMPANY_NAME=Our Company, are valid for Docker Compose but break bash.)
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '\r'; }
POSTGRES_USER=$(env_get POSTGRES_USER)
N8N_DB_NAME=$(env_get N8N_DB_NAME)
SCREENER_DB_NAME=$(env_get SCREENER_DB_NAME)
BACKUP_S3_BUCKET=$(env_get BACKUP_S3_BUCKET)

STAMP=$(date +%Y-%m-%d_%H%M)
OUT="$APP_DIR/backups"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml)

for DB in "${N8N_DB_NAME:-n8n}" "${SCREENER_DB_NAME:-screener}"; do
  FILE="$OUT/${DB}_${STAMP}.dump"
  "${COMPOSE[@]}" exec -T postgres pg_dump -U "${POSTGRES_USER:-postgres}" -d "$DB" -Fc > "$FILE"
  echo "$(date -Is) dumped $DB ($(du -h "$FILE" | cut -f1))"
  if [[ -n "${BACKUP_S3_BUCKET:-}" ]]; then
    aws s3 cp --only-show-errors "$FILE" "s3://${BACKUP_S3_BUCKET}/postgres/$(basename "$FILE")"
    echo "$(date -Is) uploaded to s3://${BACKUP_S3_BUCKET}/postgres/"
  fi
done

# Keep the 7 newest local dumps per database
for DB in "${N8N_DB_NAME:-n8n}" "${SCREENER_DB_NAME:-screener}"; do
  ls -1t "$OUT/${DB}_"*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f
done
