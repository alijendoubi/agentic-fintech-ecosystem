from __future__ import annotations

from pathlib import Path

import pytest
from grpc_tools import protoc

PROTO_DIR = Path(__file__).resolve().parents[3] / "shared" / "proto"
PROTOS = ("market_snapshot", "trade_signal", "order_request", "aegis")


@pytest.fixture(scope="session")
def generated_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the real shared protos (message + gRPC stubs) once per test session."""
    out = tmp_path_factory.mktemp("generated")
    rc = protoc.main(
        [
            "protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            *(str(PROTO_DIR / f"{name}.proto") for name in PROTOS),
        ]
    )
    assert rc == 0, "protoc failed to compile the shared protos"
    return out
