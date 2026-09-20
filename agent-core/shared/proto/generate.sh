#!/usr/bin/env bash
# Generate Python message + gRPC stubs (and .pyi typing stubs) for every .proto
# in this directory into agent-core/shared/generated (gitignored).
#
# Works on Linux, macOS and Windows Git Bash. Requires: pip install grpcio-tools
#
#   ./generate.sh                 # auto-detect a python that has grpc_tools
#   PYTHON=/path/to/python ./generate.sh
#
# Modules are generated FLAT (import trade_signal_pb2, import aegis_pb2_grpc),
# so add the output directory to PYTHONPATH / sys.path to use them.
set -euo pipefail

# Work from this script's directory and use only relative paths: native Windows
# Python cannot resolve MSYS-style /c/... paths passed as arguments.
cd "$(dirname "${BASH_SOURCE[0]}")"

OUT_DIR="../generated"

# Pick the first interpreter that can actually import grpc_tools. Trying each
# candidate also skips the Windows Store "python3" stub, which exits non-zero.
PY=""
for candidate in "${PYTHON:-}" python3 python py; do
  [ -n "$candidate" ] || continue
  if "$candidate" -c "import grpc_tools.protoc" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "error: no Python with grpcio-tools found. Run: pip install grpcio-tools" >&2
  echo "       (or set PYTHON=/path/to/python)" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"

# Deterministic file list, no reliance on shell glob quoting.
protos=()
for f in *.proto; do
  [ -e "$f" ] && protos+=("$f")
done
if [ "${#protos[@]}" -eq 0 ]; then
  echo "error: no .proto files found in $(pwd)" >&2
  exit 1
fi

"$PY" -m grpc_tools.protoc \
  -I . \
  --python_out="$OUT_DIR" \
  --pyi_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "${protos[@]}"

echo "Generated stubs for ${#protos[@]} proto files in $(cd "$OUT_DIR" && pwd)"
