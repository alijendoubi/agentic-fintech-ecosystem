#!/usr/bin/env bash
# Preflight for .env before `docker compose up` (ALI-21). `docker compose config` only proves a
# required variable is non-empty; this also refuses template values, short secrets, non-hex Redis
# passwords / Chroma token, a malformed QuestDB ILP key, a shared audit password and malformed
# resource limits (ALI-20). Services re-check their own secrets at startup; this
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

# check NAME MIN_LEN [any|hex|name|b64url] [optional]
#   hex    : [0-9a-fA-F]+ (Redis passwords sit in redis:// URLs; the Chroma token in the proxy config)
#   name   : [A-Za-z0-9._-]+ (user names; HTTP basic auth forbids ':')
#   b64url : exactly 43 base64url chars (a P-256 private scalar without padding)
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
    fail "$name must be hex; use openssl rand -hex 24"
  elif [ "$kind" = name ] && ! [[ "$value" =~ ^[A-Za-z0-9._-]+$ ]]; then
    fail "$name may only contain letters, digits, '.', '_' and '-'"
  elif [ "$kind" = b64url ] && ! [[ "$value" =~ ^[A-Za-z0-9_-]{43}$ ]]; then
    fail "$name must be 43 base64url characters (the P-256 private scalar, no padding)"
  fi
}

# Resource limits (owner decision, ALI-20): required, no defaults. Values are not judged, only
# their form: compose would otherwise fail late or, for pids, accept -1 (= unlimited).
check_limits() {
  local prefix="$1" mem cpus pids
  mem="${VALUES[${prefix}_MEM_LIMIT]-}"
  cpus="${VALUES[${prefix}_CPUS]-}"
  pids="${VALUES[${prefix}_PIDS_LIMIT]-}"
  if ! [[ "$mem" =~ ^[1-9][0-9]*[kmg]$ ]]; then
    fail "${prefix}_MEM_LIMIT must be a positive integer with a k/m/g suffix (e.g. 512m)"
  fi
  if ! [[ "$cpus" =~ ^[0-9]+(\.[0-9]+)?$ ]] || ! [[ "$cpus" =~ [1-9] ]]; then
    fail "${prefix}_CPUS must be a positive decimal number (e.g. 0.5)"
  fi
  if ! [[ "$pids" =~ ^[1-9][0-9]*$ ]]; then
    fail "${prefix}_PIDS_LIMIT must be a positive integer (it counts threads too)"
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
# Broker credentials: required by broker-gateway, the only service that receives them (ADR-004).
check ALPACA_API_KEY 1
check ALPACA_SECRET_KEY 1
check HITL_API_TOKEN 16 any optional
# QuestDB (ALI-20): HTTP basic auth + PG wire credentials, ILP ECDSA key; Chroma bearer token.
check QUESTDB_HTTP_USER 1 name
check QUESTDB_HTTP_PASSWORD 16
check QUESTDB_PG_USER 1 name
check QUESTDB_PG_PASSWORD 16
check QUESTDB_ILP_AUTH_KEY_ID 1 name
check QUESTDB_ILP_AUTH_TOKEN 43 b64url
check CHROMA_AUTH_TOKEN 32 hex
for service in COGNITIVE_CORE LLM_GATEWAY REGIME_DETECTOR VECTOR_DB VECTOR_DB_STORE SENSORY_ARRAY AEGIS \
  AEGIS_SUPERVISOR EXECUTION_MOTOR BROKER_GATEWAY REFDATA_BRIDGE QUESTDB REDIS POSTGRES_AUDIT HITL_BACKEND \
  HITL_INTERFACE HITL_PROXY; do
  check_limits "$service"
done

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

# QuestDB ILP auth.conf (public keys): when present, it must list QUESTDB_ILP_AUTH_KEY_ID, or
# sensory-array can never authenticate. The dev override mounts dev-tls/out/questdb-ilp instead.
ilp_dir="${VALUES[QUESTDB_ILP_AUTH_DIR]:-./secrets/questdb-ilp-auth}"
case "$ilp_dir" in /*) ;; *) ilp_dir="$(dirname "$ENV_FILE")/$ilp_dir" ;; esac
kid="${VALUES[QUESTDB_ILP_AUTH_KEY_ID]-}"
if [ -f "$ilp_dir/auth.conf" ] && [ -n "$kid" ]; then
  if ! awk -v k="$kid" '$1 == k && $2 == "ec-p-256-sha256" { found = 1 } END { exit !found }' "$ilp_dir/auth.conf"; then
    fail "$ilp_dir/auth.conf has no ec-p-256-sha256 entry for QUESTDB_ILP_AUTH_KEY_ID"
  fi
fi

if [ "$PROBLEMS" -gt 0 ]; then
  echo "check-env: $PROBLEMS problem(s) in $ENV_FILE" >&2
  exit 1
fi
echo "check-env: $ENV_FILE passed"
