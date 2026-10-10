#!/bin/sh
# Runs ONCE, when the postgres volume is empty (first `docker compose up`).
# Creates a separate database + user for n8n and for the screener API, so
# each service can only touch its own data (least privilege).
#
# To re-run it you must delete the volume (this ERASES all data):
#   docker compose down -v
set -e

create_db() {
  db="$1"; user="$2"; pass="$3"
  echo "init: creating role '$user' and database '$db'"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
       -v db="$db" -v usr="$user" -v pwd="$pass" <<'SQL'
CREATE ROLE :"usr" WITH LOGIN PASSWORD :'pwd';
CREATE DATABASE :"db" OWNER :"usr";
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
SQL
}

create_db "$N8N_DB_NAME" "$N8N_DB_USER" "$N8N_DB_PASSWORD"
create_db "$SCREENER_DB_NAME" "$SCREENER_DB_USER" "$SCREENER_DB_PASSWORD"
