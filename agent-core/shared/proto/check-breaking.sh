#!/usr/bin/env bash
# Detect breaking changes in the protos versus a git ref.
#
#   ./check-breaking.sh                        # against origin/main
#   BASE_REF=chore/base-fixes ./check-breaking.sh
#
# How it works: the protos at $BASE_REF are exported with `git archive` into a
# temp dir and used as the buf baseline (`buf breaking --against <dir>`). This
# avoids buf's own git-input mode, which needs the .git directory visible to
# buf and therefore does not work from a container or from a git worktree.
# Rules come from buf.yaml (`breaking.use: FILE`).
#
# Uses a local `buf` binary if present, otherwise the bufbuild/buf docker image.
# Works on Linux, macOS and Windows Git Bash.
set -euo pipefail

BASE_REF="${BASE_REF:-origin/main}"
PROTO_REL="agent-core/shared/proto"

cd "$(dirname "${BASH_SOURCE[0]}")"
PROTO_DIR="$(pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel)"

BASE_DIR="$(mktemp -d)"
trap 'rm -rf "$BASE_DIR"' EXIT

if ! git -C "$REPO_ROOT" rev-parse --verify --quiet "$BASE_REF^{commit}" >/dev/null; then
  echo "error: git ref '$BASE_REF' not found (set BASE_REF, fetch full history in CI)" >&2
  exit 2
fi

git -C "$REPO_ROOT" archive "$BASE_REF" -- "$PROTO_REL" | tar -x -C "$BASE_DIR"
BASELINE="$BASE_DIR/$PROTO_REL"
if ! ls "$BASELINE"/*.proto >/dev/null 2>&1; then
  echo "error: no .proto files at $BASE_REF:$PROTO_REL" >&2
  exit 2
fi

echo "buf breaking: $PROTO_REL (working tree) against $BASE_REF"

if command -v buf >/dev/null 2>&1; then
  buf breaking "$PROTO_DIR" --against "$BASELINE"
else
  # Docker path. On Windows Git Bash, convert to native paths for -v and stop
  # MSYS rewriting the container paths.
  if command -v cygpath >/dev/null 2>&1; then
    HOST_PROTO="$(cygpath -m "$PROTO_DIR")"
    HOST_BASE="$(cygpath -m "$BASELINE")"
  else
    HOST_PROTO="$PROTO_DIR"
    HOST_BASE="$BASELINE"
  fi
  MSYS_NO_PATHCONV=1 docker run --rm \
    -v "$HOST_PROTO:/workspace:ro" \
    -v "$HOST_BASE:/baseline:ro" \
    -w /workspace bufbuild/buf breaking /workspace --against /baseline
fi

echo "OK: no breaking changes versus $BASE_REF"
