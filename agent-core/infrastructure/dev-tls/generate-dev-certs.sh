#!/bin/sh
# generate-dev-certs.sh
#
# ============================================================================
#  DEV ONLY. NOT FOR PRODUCTION. DO NOT COPY THIS SCRIPT INTO A PROD RUNBOOK.
# ============================================================================
#
# Generates a throwaway CA plus one server certificate (aegis) and four client
# certificates (cognitive-core, execution-motor, refdata-bridge,
# aegis-supervisor) for running the Agentic Fintech Ecosystem dev compose
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
dev compose stack. Regenerates a throwaway CA, a server cert for "aegis",
and client certs for cognitive-core, execution-motor, refdata-bridge and
aegis-supervisor. Never use this for anything other than a developer's own
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
# 2. Server cert for aegis itself
# ----------------------------------------------------------------------------
issue_cert server aegis aegis "DNS:aegis"

# ----------------------------------------------------------------------------
# 3. Client certs — CN must match the "peers" keys in identities.json
#    (Aegis maps the verified client certificate CN, else first DNS/URI SAN,
#    to RPC roles; see agent-core/zone-b/aegis/README.md "Identities file").
# ----------------------------------------------------------------------------
issue_cert client cognitive-core     cognitive-core     ""
issue_cert client execution-motor    execution-motor    ""
issue_cert client refdata-bridge     refdata-bridge     ""
issue_cert client aegis-supervisor   aegis-supervisor   ""

# ----------------------------------------------------------------------------
# 4. Per-directory DEV ONLY README (also see ./README.md for the full guide)
# ----------------------------------------------------------------------------
for d in aegis cognitive-core execution-motor refdata-bridge aegis-supervisor; do
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
