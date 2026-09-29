"""Compile broker_gateway.proto (messages + gRPC stubs) into a tmp dir at test time; nothing
is written to the repo."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROTO_DIR = Path(__file__).resolve().parents[3] / "shared" / "proto"


@pytest.fixture(scope="session")
def protos(tmp_path_factory: pytest.TempPathFactory) -> tuple[ModuleType, ModuleType]:
    out = tmp_path_factory.mktemp("generated")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            str(PROTO_DIR / "broker_gateway.proto"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"protoc failed: {result.stderr}")
    sys.path.insert(0, str(out))
    try:
        return (
            importlib.import_module("broker_gateway_pb2"),
            importlib.import_module("broker_gateway_pb2_grpc"),
        )
    finally:
        sys.path.remove(str(out))


@pytest.fixture
def pb2(protos: tuple[ModuleType, ModuleType]) -> ModuleType:
    return protos[0]
