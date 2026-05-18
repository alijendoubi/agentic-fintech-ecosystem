#!/usr/bin/env bash
# Generate Python gRPC stubs from .proto files.
# Requires: pip install grpcio-tools
set -euo pipefail

PROTO_DIR="$(dirname "$0")"
OUT_DIR="$PROTO_DIR/../generated"
mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
  -I "$PROTO_DIR" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$PROTO_DIR"/*.proto

echo "Stubs generated in $OUT_DIR"
