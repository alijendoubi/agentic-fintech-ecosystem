"""Compile the protos into a tmp dir at test time (nothing is written to the repo).

Uses shared/proto once it contains aegis.proto (integration/wave1, bed7eef+). Until then the
copies in tests/proto_fixtures (verbatim from integration/wave1) are compiled instead.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SHARED = Path(__file__).resolve().parents[3] / "shared" / "proto"
_FIXTURES = Path(__file__).resolve().parent / "proto_fixtures"
PROTO_DIR = _SHARED if (_SHARED / "aegis.proto").exists() else _FIXTURES


@pytest.fixture(scope="session")
def pb2(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType]:
    out = tmp_path_factory.mktemp("generated")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            *(str(f) for f in sorted(PROTO_DIR.glob("*.proto"))),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"protoc failed: {result.stderr}")
    sys.path.insert(0, str(out))
    try:
        return {
            "order": importlib.import_module("order_request_pb2"),
            "aegis": importlib.import_module("aegis_pb2"),
        }
    finally:
        sys.path.remove(str(out))
