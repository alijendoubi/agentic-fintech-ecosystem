#!/bin/sh
# generate-dev-certs.sh
#
# ============================================================================
#  DEV ONLY. NOT FOR PRODUCTION. DO NOT COPY THIS SCRIPT INTO A PROD RUNBOOK.
# ============================================================================
#
# Generates a throwaway CA plus server certificates (aegis, execution-motor,
# hitl-proxy) and client certificates (cognitive-core, execution-motor,
# refdata-bridge, aegis-supervisor, hitl-backend, operator) for running the Agentic Fintech Ecosystem dev compose
# stack (docker-compose.dev.yml) with real mTLS instead of Aegis's plaintext
# AEGIS_INSECURE_DEV mode.
#
# All key material is short-lived (30 days), weak on purpose for speed (P-256
# is fine, but validity/rotation discipline is NOT production-grade), and is
# never committed to git (see ../../../.gitignore: *.pem/*.key/secrets/).
#
# Usage:
#   ./generate-dev-certs.sh --yes-i-know-this-is-dev-only [--days N] [--out DIR]
#
# The --yes-i-know-this-is-dev-only flag is mandatory. Its only purpose is to
# stop this script from being copy-pasted into a production runbook and run
# unattended: there is no other guard that can reliably detect "production"
# from inside a shell script, so the explicit human acknowledgement IS the
# guard.
#
# Idempotency: every run WIPES and regenerates the entire output directory
# from a brand-new CA. That is deliberate — a dev CA has no reason to persist
# across runs, and silently reusing a stale CA while regenerating only some
# leaf certs would produce a directory where some client certs verify against
# the CA on disk and others don't. Each run prints the new CA fingerprint so
# you can tell containers to be restarted after a regeneration.

set -eu

# On Windows Git Bash, MSYS rewrites any argument that looks like a leading-
# slash unix path (e.g. our openssl "/O=.../CN=..." -subj value) into a
# Windows path, which corrupts -subj. Excluding args starting with "/O=" from
# that rewrite fixes it there and is a no-op everywhere else (plain POSIX
# shells don't consult this variable at all).
MSYS2_ARG_CONV_EXCL="${MSYS2_ARG_CONV_EXCL:-}/O="
export MSYS2_ARG_CONV_EXCL

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT_DIR="$SCRIPT_DIR/out"
DAYS=30
CONFIRMED=0

usage() {
    cat <<'EOF'
generate-dev-certs.sh --yes-i-know-this-is-dev-only [--days N] [--out DIR]

DEV ONLY mTLS certificate bootstrap for the Agentic Fintech Ecosystem local
dev compose stack. Regenerates a throwaway CA, server certs for aegis,
execution-motor and hitl-proxy, and the client certs listed in README.md.
Never use this for anything other than a developer's own
machine.

  --yes-i-know-this-is-dev-only   required; explicit human acknowledgement
  --days N                        cert validity in days (default: 30)
  --out DIR                       output directory (default: ./out)
  -h, --help                      show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes-i-know-this-is-dev-only)
            CONFIRMED=1
            shift
            ;;
        --days)
            DAYS="$2"
            shift 2
            ;;
        --out)
            OUT_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "generate-dev-certs.sh: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ "$CONFIRMED" -ne 1 ]; then
    cat >&2 <<'EOF'
generate-dev-certs.sh: refusing to run.

This script generates DEV-ONLY mTLS certificates with no real security
guarantees (short-lived, weak passphrase-less keys, a throwaway CA). It must
never be run against a production or staging environment, and must never be
wired into a production deployment pipeline.

If you are certain this is a local developer machine, re-run with:
  ./generate-dev-certs.sh --yes-i-know-this-is-dev-only
EOF
    exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
    echo "generate-dev-certs.sh: openssl not found on PATH" >&2
    exit 1
fi

echo "============================================================"
echo " DEV ONLY certificate bootstrap - NOT FOR PRODUCTION"
echo " Output directory : $OUT_DIR"
echo " Validity          : $DAYS days"
echo "============================================================"

if [ -d "$OUT_DIR" ]; then
    echo "generate-dev-certs.sh: '$OUT_DIR' already exists - wiping and" \
         "regenerating a FRESH CA (old certs signed by the previous CA" \
         "become invalid; restart any containers using them)."
    rm -rf "$OUT_DIR"
fi
mkdir -p "$OUT_DIR"

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

# ----------------------------------------------------------------------------
# 1. Dev CA
# ----------------------------------------------------------------------------
CA_DIR="$OUT_DIR/ca"
mkdir -p "$CA_DIR"

openssl ecparam -name prime256v1 -genkey -noout -out "$CA_DIR/ca.key"
openssl req -x509 -new -key "$CA_DIR/ca.key" -sha256 -days "$DAYS" \
    -subj "/O=AFE Dev Only/CN=afe-dev-ca" \
    -addext "basicConstraints=critical,CA:true" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$CA_DIR/ca.pem"

CA_FINGERPRINT=$(openssl x509 -in "$CA_DIR/ca.pem" -noout -fingerprint -sha256)
echo "Generated dev CA: $CA_FINGERPRINT"

# ----------------------------------------------------------------------------
# helper: issue a leaf cert
#   issue_cert <role: server|client> <identity-dir-name> <CN> <SAN-extra>
# ----------------------------------------------------------------------------
issue_cert() {
    kind="$1"      # "server" or "client"
    ident="$2"     # output subdirectory name, e.g. "aegis"
    cn="$3"        # certificate CN
    san="$4"       # subjectAltName value, e.g. "DNS:aegis"

    ident_dir="$OUT_DIR/$ident"
    mkdir -p "$ident_dir"

    key_out="$ident_dir/$([ "$kind" = server ] && echo server.key || echo client.key)"
    csr_out="$WORK_DIR/$ident.csr"
    ext_out="$WORK_DIR/$ident.ext"
    cert_out="$ident_dir/$([ "$kind" = server ] && echo server.pem || echo client.pem)"

    openssl ecparam -name prime256v1 -genkey -noout -out "$key_out"
    openssl req -new -key "$key_out" -subj "/O=AFE Dev Only/CN=$cn" -out "$csr_out"

    if [ "$kind" = server ]; then
        eku="serverAuth"
    else
        eku="clientAuth"
    fi

    {
        echo "basicConstraints=critical,CA:false"
        echo "keyUsage=critical,digitalSignature,keyEncipherment"
        echo "extendedKeyUsage=$eku"
        if [ -n "$san" ]; then
            echo "subjectAltName=$san"
        fi
    } > "$ext_out"

    openssl x509 -req -in "$csr_out" -CA "$CA_DIR/ca.pem" -CAkey "$CA_DIR/ca.key" \
        -CAcreateserial -CAserial "$WORK_DIR/ca.srl" \
        -days "$DAYS" -sha256 -extfile "$ext_out" -out "$cert_out"

    # Every identity gets its own copy of the CA cert alongside its leaf, so
    # each service's mounted directory is self-contained (matches the
    # AEGIS_TLS_DIR / AEGIS_SUPERVISOR_TLS_DIR layout in docker-compose.yml).
    cp "$CA_DIR/ca.pem" "$ident_dir/ca.pem"

    rm -f "$key_out.pub" 2>/dev/null || true
    chmod 600 "$key_out" 2>/dev/null || true

    echo "  issued $kind cert: CN=$cn identity-dir=$ident_dir SAN=[$san]"
}

# ----------------------------------------------------------------------------
# 2. Server certs for services that run their own mTLS listener
# ----------------------------------------------------------------------------
issue_cert server aegis aegis "DNS:aegis"
issue_cert server execution-motor execution-motor "DNS:execution-motor"
# ADR-004: the broker gateway's listener. Its only allowed client is CN=execution-motor (the
# execution-motor client cert below, reused for this channel).
issue_cert server broker-gateway broker-gateway "DNS:broker-gateway"

# hitl-proxy (TLS in front of the HITL terminal and hitl-backend, owner decision 2026-09-29):
# internal.pem for the listener the terminal calls (https://hitl-proxy:9443), then server.pem for the
# operator listener the browser uses (https://localhost:8443). Layout matches ${HITL_TLS_DIR}.
issue_cert server hitl-proxy hitl-proxy "DNS:hitl-proxy"
mv "$OUT_DIR/hitl-proxy/server.pem" "$OUT_DIR/hitl-proxy/internal.pem"
mv "$OUT_DIR/hitl-proxy/server.key" "$OUT_DIR/hitl-proxy/internal.key"
issue_cert server hitl-proxy localhost "DNS:localhost,IP:127.0.0.1,IP:::1"
# The proxy container runs as uid 101 (nginx) and reads its keys through a bind mount; 0600 root-owned
# (or host-user-owned) keys are unreadable there. DEV ONLY: make them world-readable.
chmod 644 "$OUT_DIR/hitl-proxy/internal.key" "$OUT_DIR/hitl-proxy/server.key"

# ----------------------------------------------------------------------------
# 3. Client certs — CN must match the "peers" keys in identities.json
#    (Aegis maps the verified client certificate CN, else first DNS/URI SAN,
#    to RPC roles; see agent-core/zone-b/aegis/README.md "Identities file").
# ----------------------------------------------------------------------------
issue_cert client cognitive-core     cognitive-core     ""
issue_cert client execution-motor    execution-motor    ""
issue_cert client refdata-bridge     refdata-bridge     ""
issue_cert client aegis-supervisor   aegis-supervisor   ""
issue_cert client hitl-backend       hitl-backend       ""

# ----------------------------------------------------------------------------
# 3b. DEV ONLY attestation signing key (ALI-167)
#     Aegis's dev signer (AEGIS_SIGNER=dev) otherwise generates a random key in
#     memory at every start, which execution-motor cannot know, so no approved
#     decision could ever verify at the motor. Pin it: a 32-byte Ed25519 seed for
#     Aegis (AEGIS_DEV_SIGNING_SEED_FILE) and the matching public key for the
#     motor (MOTOR_ATTESTATION_KEYS_FILE). key_id follows Aegis's dev signer:
#     "dev-ed25519-" + first 16 hex chars of SHA-256(raw public key).
# ----------------------------------------------------------------------------
hex_of() { od -An -v -tx1 | tr -d ' 
'; }

SIGNER_DIR="$OUT_DIR/aegis-signer"
mkdir -p "$SIGNER_DIR"
openssl genpkey -algorithm ed25519 -out "$WORK_DIR/signer.pem"
SEED_HEX=$(openssl pkey -in "$WORK_DIR/signer.pem" -outform DER | tail -c 32 | hex_of)
openssl pkey -in "$WORK_DIR/signer.pem" -pubout -outform DER | tail -c 32 > "$WORK_DIR/signer.pub.raw"
PUB_HEX=$(hex_of < "$WORK_DIR/signer.pub.raw")
FPR_HEX=$(openssl dgst -sha256 -r "$WORK_DIR/signer.pub.raw" | cut -c1-16)
if [ ${#SEED_HEX} -ne 64 ] || [ ${#PUB_HEX} -ne 64 ] || [ ${#FPR_HEX} -ne 16 ]; then
    echo "error: could not derive the dev Ed25519 signing key" >&2
    exit 1
fi
KEY_ID="dev-ed25519-$FPR_HEX"
printf '%s
' "$SEED_HEX" > "$SIGNER_DIR/seed.hex"
chmod 600 "$SIGNER_DIR/seed.hex" 2>/dev/null || true
printf '[{"key_id": "%s", "algorithm": "ED25519", "public_key_hex": "%s"}]
'     "$KEY_ID" "$PUB_HEX" > "$OUT_DIR/execution-motor/attestation-keys.json"
# The broker gateway verifies the same attestations with its own copy of the public key.
cp "$OUT_DIR/execution-motor/attestation-keys.json" "$OUT_DIR/broker-gateway/attestation-keys.json"
echo "  dev attestation signer: key_id=$KEY_ID (seed in $SIGNER_DIR, public key in execution-motor/)"

# ----------------------------------------------------------------------------
# 3d. DEV ONLY QuestDB ILP auth key (ALI-20)
#     QuestDB's ILP/TCP listener authenticates writers with an ECDSA P-256 key:
#     the server holds the PUBLIC key in auth.conf (QDB_LINE_TCP_AUTH_DB_PATH),
#     sensory-array holds the private scalar `d` (QUESTDB_ILP_AUTH_TOKEN, a
#     secret that compose reads from .env). The SEC1 DER of a P-256 key with
#     named-curve parameters is exactly 121 bytes: d is bytes 8-39, the public
#     point x||y is the last 64 bytes. Values are base64url without padding.
# ----------------------------------------------------------------------------
b64url() { openssl base64 -A | tr '+/' '-_' | tr -d '='; }

QDB_ILP_DIR="$OUT_DIR/questdb-ilp"
mkdir -p "$QDB_ILP_DIR"
openssl ecparam -name prime256v1 -genkey -noout -out "$WORK_DIR/qdb-ilp.pem"
openssl ec -in "$WORK_DIR/qdb-ilp.pem" -outform DER -out "$WORK_DIR/qdb-ilp.der" 2>/dev/null
if [ "$(wc -c < "$WORK_DIR/qdb-ilp.der" | tr -d ' ')" -ne 121 ]; then
    echo "error: unexpected P-256 DER layout for the QuestDB ILP key" >&2
    exit 1
fi
QDB_ILP_D=$(tail -c +8 "$WORK_DIR/qdb-ilp.der" | head -c 32 | b64url)
QDB_ILP_X=$(tail -c 64 "$WORK_DIR/qdb-ilp.der" | head -c 32 | b64url)
QDB_ILP_Y=$(tail -c 32 "$WORK_DIR/qdb-ilp.der" | b64url)
if [ ${#QDB_ILP_D} -ne 43 ] || [ ${#QDB_ILP_X} -ne 43 ] || [ ${#QDB_ILP_Y} -ne 43 ]; then
    echo "error: could not derive the QuestDB ILP key" >&2
    exit 1
fi
printf 'sensory-array ec-p-256-sha256 %s %s\n' "$QDB_ILP_X" "$QDB_ILP_Y" > "$QDB_ILP_DIR/auth.conf"
chmod 644 "$QDB_ILP_DIR/auth.conf" 2>/dev/null || true # read by the questdb user (uid 10001)
printf '%s\n' "$QDB_ILP_D" > "$QDB_ILP_DIR/ilp-token"
chmod 600 "$QDB_ILP_DIR/ilp-token" 2>/dev/null || true
echo "  dev QuestDB ILP key: kid=sensory-array; put the content of $QDB_ILP_DIR/ilp-token"
echo "    into .env as QUESTDB_ILP_AUTH_TOKEN (and QUESTDB_ILP_AUTH_KEY_ID=sensory-array)"

# ----------------------------------------------------------------------------
# 4. Per-directory DEV ONLY README (also see ./README.md for the full guide)
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# 3c. DEV ONLY operator identity and kill-switch reset approvers (ALI-170)
#     Without them no kill switch can ever be reset in the dev stack: a reset needs
#     a peer holding the kill-reset role PLUS signed approvals from registered
#     approvers (operator / compliance; count per level: spec 5.2). Writes
#     out/identities.json (the dev AEGIS_IDENTITIES_FILE) and one private Ed25519
#     seed per approver under out/approvers/ (hex; used to sign reset approvals).
# ----------------------------------------------------------------------------
issue_cert client operator operator ""

APPROVER_DIR="$OUT_DIR/approvers"
mkdir -p "$APPROVER_DIR"
approver_json=""
for spec in "dev-operator-a:operator" "dev-operator-b:operator" "dev-compliance:compliance"; do
    id=${spec%%:*}
    role=${spec#*:}
    openssl genpkey -algorithm ed25519 -out "$WORK_DIR/$id.pem"
    openssl pkey -in "$WORK_DIR/$id.pem" -outform DER | tail -c 32 | hex_of > "$APPROVER_DIR/$id.seed"
    chmod 600 "$APPROVER_DIR/$id.seed" 2>/dev/null || true
    pub=$(openssl pkey -in "$WORK_DIR/$id.pem" -pubout -outform DER | tail -c 32 | hex_of)
    if [ ${#pub} -ne 64 ]; then
        echo "error: could not derive approver key $id" >&2
        exit 1
    fi
    entry=$(printf '"%s": {"roles": ["%s"], "ed25519_pubkey_hex": "%s"}' "$id" "$role" "$pub")
    approver_json="${approver_json:+$approver_json, }$entry"
done
cat > "$OUT_DIR/identities.json" <<EOF
{
  "peers": {
    "cognitive-core": ["signal-submitter"],
    "execution-motor": ["state-reader", "execution-reporter"],
    "refdata-bridge": ["market-data-writer"],
    "aegis-supervisor": ["state-reader", "kill-trigger"],
    "hitl-backend": ["hold-resolver"],
    "operator": ["state-reader", "kill-trigger", "kill-reset"]
  },
  "approvers": {$approver_json}
}
EOF
echo "  dev operator identity + 3 reset approvers: $OUT_DIR/identities.json, seeds in $APPROVER_DIR"

for d in aegis aegis-signer cognitive-core execution-motor broker-gateway refdata-bridge aegis-supervisor hitl-backend hitl-proxy operator approvers questdb-ilp; do
    cat > "$OUT_DIR/$d/README.md" <<EOF
# DEV ONLY - NOT FOR PRODUCTION

Generated by generate-dev-certs.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
Validity: $DAYS days. CA fingerprint: $CA_FINGERPRINT

This directory holds throwaway mTLS material for the "$d" identity in the
local docker-compose.dev.yml stack only. Never reuse these files outside a
developer's own machine, and never commit them (they are git-ignored).
Re-run generate-dev-certs.sh to rotate; that wipes and replaces every
identity's certs at once because they all chain to the same CA.
EOF
done

echo "============================================================"
echo " Done. DEV ONLY certificates written under: $OUT_DIR"
echo " Re-run this script any time to rotate all certs (wipes + regenerates)."
echo "============================================================"
