#!/usr/bin/env bash
# Audit database initialisation. Mount the whole sql/ directory at /docker-entrypoint-initdb.d (the postgres image
# runs this top-level .sh on first initialisation only; the migrations/ subdirectory is not auto-executed).
#
# Required environment (fail closed if missing):
#   POSTGRES_USER / POSTGRES_DB       bootstrap superuser + database (standard postgres image variables)
#   AFE_AUDIT_OWNER_PASSWORD          password of afe_audit_owner (migrator; services must NEVER receive it)
#   AFE_AUDIT_APP_PASSWORD            password of afe_audit_app   (the only credential services receive)
# Optional: AFE_AUDIT_SQL_DIR (defaults to ./migrations next to this script).
set -euo pipefail

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${AFE_AUDIT_OWNER_PASSWORD:?AFE_AUDIT_OWNER_PASSWORD is required}"
: "${AFE_AUDIT_APP_PASSWORD:?AFE_AUDIT_APP_PASSWORD is required}"

AFE_AUDIT_SQL_DIR="${AFE_AUDIT_SQL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/migrations}"
export AFE_AUDIT_OWNER_PASSWORD AFE_AUDIT_APP_PASSWORD

run_sql() { # $1 = role, $2 = file
  psql -v ON_ERROR_STOP=1 --no-psqlrc --username "$1" --dbname "$POSTGRES_DB" --file "$AFE_AUDIT_SQL_DIR/$2"
}

run_sql "$POSTGRES_USER" 000_bootstrap_roles.sql   # superuser: roles + database privileges
run_sql afe_audit_owner  001_audit_events.sql      # owner: table, triggers, grants
run_sql afe_audit_owner  002_verify_chain.sql      # owner: verification function
run_sql "$POSTGRES_USER" 900_ddl_guard.sql         # superuser: DDL lock for non-superusers
