"""ALI-48 e2e rig. No network: sockets are blocked, the broker is a mock, Aegis is a fixture.

The attestation fixture is produced by the REAL Aegis crate
(agent-core/zone-b/execution-motor/tests/fixtures/gen_aegis_fixtures.sh), not hand-built.
"""

from __future__ import annotations

import importlib
import json
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

AGENT_CORE = Path(__file__).resolve().parents[2]
MOTOR_DIR = AGENT_CORE / "zone-b" / "execution-motor"
PROTO_DIR = AGENT_CORE / "shared" / "proto"
FIXTURE = MOTOR_DIR / "tests" / "fixtures" / "aegis_attestations.json"

# `execution-motor` has a hyphen: put its directory on sys.path to import `execution_motor`.
sys.path.insert(0, str(MOTOR_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="session")
def pb2(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType]:
    """Generated protobuf modules, compiled into a tmp dir (nothing is written to the repo)."""
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
            "signal": importlib.import_module("trade_signal_pb2"),
        }
    finally:
        sys.path.remove(str(out))


@pytest.fixture(scope="session")
def aegis_fixture() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted in an e2e test")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
