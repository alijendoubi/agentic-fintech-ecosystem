from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROTO_DIR = Path(__file__).resolve().parents[3] / "shared" / "proto"
MODULES = (
    "aegis_pb2",
    "aegis_pb2_grpc",
    "trade_signal_pb2",
    "market_snapshot_pb2",
    "compliance_manifest_pb2",
    "execution_motor_pb2",
    "execution_motor_pb2_grpc",
)


@pytest.fixture(scope="session")
def pb(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType]:
    out = tmp_path_factory.mktemp("generated")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            *(str(p) for p in sorted(PROTO_DIR.glob("*.proto"))),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"protoc failed: {result.stderr}")
    sys.path.insert(0, str(out))
    return {name: importlib.import_module(name) for name in MODULES}
