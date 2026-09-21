"""`compliance-manifest` has a hyphen in its name (not importable as a dotted package): put this
directory on
sys.path so `afe_manifest` imports flat. Protobuf stubs are compiled from the REAL protos in
agent-core/shared/proto into a temp dir at test time (never committed) with grpc_tools.protoc."""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_HERE = Path(__file__).resolve().parent
PROTO_DIR = _HERE.parents[1] / "shared" / "proto"
AUDIT_LOGGER_DIR = _HERE.parent / "audit-logger"
for _p in (_HERE, _HERE / "tests", AUDIT_LOGGER_DIR, AUDIT_LOGGER_DIR / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

PROTO_FILES = ("market_snapshot", "trade_signal", "order_request", "compliance_manifest")


@pytest.fixture(scope="session")
def pb(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SimpleNamespace]:
    out = tmp_path_factory.mktemp("proto_stubs")
    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I{PROTO_DIR}",
        f"--python_out={out}",
        *[str(PROTO_DIR / f"{name}.proto") for name in PROTO_FILES],
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"protoc failed: {result.stderr}")
    sys.path.insert(0, str(out))
    try:
        modules: dict[str, Any] = {
            name: importlib.import_module(f"{name}_pb2") for name in PROTO_FILES
        }
        yield SimpleNamespace(**modules)
    finally:
        sys.path.remove(str(out))
