#!/usr/bin/env bash
# Preflight for .env before `docker compose up` (ALI-21). `docker compose config` only proves a
# required variable is non-empty; this also refuses template values, short secrets, non-hex Redis
# passwords and a shared audit password. Services re-check their own secrets at startup; this
# catches the whole file at once, before any container (or database volume) is created.
#
#   bash check-env.sh            # checks ./.env
#   bash check-env.sh path/.env
#
# The file is parsed, never sourced. Values are never printed. Exit 0 = pass, 1 = at least one problem.
set -euo pipefail

ENV_FILE="${1:-$(dirname "${BASH_SOURCE[0]}")/.env}"
if [ ! -f "$ENV_FILE" ]; then
  echo "check-env: $ENV_FILE not found (cp .env.example .env and fill it)" >&2
  exit 1
fi

declare -A VALUES=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in '' | '#'*) continue ;; esac
  key="${line%%=*}"
  [ "$key" = "$line" ] && continue
  value="${line#*=}"
  # Strip one level of matching surrounding quotes, as compose does.
  if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
  VALUES["$key"]="$value"
done <"$ENV_FILE"

PROBLEMS=0
fail() {
  echo "check-env: $1" >&2
  PROBLEMS=$((PROBLEMS + 1))
}

# Same fragments as zone-c/hitl-interface/src/lib/config.ts.
is_placeholder() {
  local lowered
  lowered="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    *change-me* | *change_me* | *changeme* | *replace-me* | *replaceme* | *placeholder* | \
      *your-secret* | *yoursecret* | *example* | *default* | *password*) return 0 ;;
  esac
  return 1
}

# check NAME MIN_LEN [hex] [optional]
check() {
  local name="$1" min="$2" kind="${3:-any}" presence="${4:-required}"
  local value="${VALUES[$name]-}"
  if [ -z "$value" ]; then
    [ "$presence" = optional ] || fail "$name is empty or missing"
    return 0
  fi
  if is_placeholder "$value"; then
    fail "$name looks like a placeholder; set a random value"
  elif [ "${#value}" -lt "$min" ]; then
    fail "$name must be at least $min characters"
  elif [ "$kind" = hex ] && ! [[ "$value" =~ ^[0-9a-fA-F]+$ ]]; then
    fail "$name must be hex (it is embedded in a redis:// URL); use openssl rand -hex 24"
  fi
}

check MODEL_HMAC_KEY 32
check LLM_GATEWAY_MASTER_KEY 32
check LLM_GATEWAY_AWS_REGION 1
for role in BLUE RED JUDGE COMPRESSION REFLECTOR; do
  for kind in RPM TPM; do
    name="LLM_GATEWAY_${role}_${kind}"
    value="${VALUES[$name]-}"
    if ! [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
      fail "$name must be a positive integer (owner-chosen gateway rate limit)"
    fi
  done
done
check POLYGON_API_KEY 1
check HITL_JWT_SECRET 32
check AUDIT_DB_PASSWORD 16
check AFE_AUDIT_OWNER_PASSWORD 16
check AFE_AUDIT_APP_PASSWORD 16
for service in SENSORY REGIME COGNITIVE REFDATA HEALTHCHECK; do
  check "REDIS_${service}_PASSWORD" 32 hex
done
# Optional: only checked when set (the dev stack runs the motor on its mock broker).
check ALPACA_API_KEY 1 any optional
check ALPACA_SECRET_KEY 1 any optional
check HITL_API_TOKEN 16 any optional

if [ -n "${VALUES[AFE_AUDIT_OWNER_PASSWORD]-}" ] &&
  [ "${VALUES[AFE_AUDIT_OWNER_PASSWORD]-}" = "${VALUES[AFE_AUDIT_APP_PASSWORD]-}" ]; then
  fail "AFE_AUDIT_OWNER_PASSWORD and AFE_AUDIT_APP_PASSWORD must differ"
fi

# Aegis reads its HSM PIN from a file (never the environment); check it when it is present.
config_dir="${VALUES[AEGIS_CONFIG_DIR]:-./secrets/aegis-config}"
case "$config_dir" in /*) ;; *) config_dir="$(dirname "$ENV_FILE")/$config_dir" ;; esac
if [ -f "$config_dir/hsm_pin" ]; then
  pin="$(tr -d '\r\n' <"$config_dir/hsm_pin")"
  if [ -z "$pin" ]; then
    fail "$config_dir/hsm_pin is empty"
  elif is_placeholder "$pin"; then
    fail "$config_dir/hsm_pin looks like a placeholder"
  fi
fi

if [ "$PROBLEMS" -gt 0 ]; then
  echo "check-env: $PROBLEMS problem(s) in $ENV_FILE" >&2
  exit 1
fi
echo "check-env: $ENV_FILE passed"
