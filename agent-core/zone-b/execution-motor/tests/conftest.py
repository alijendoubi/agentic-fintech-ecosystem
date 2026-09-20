"""Compile the shared protos into a tmp dir at test time (nothing is written to the repo)."""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROTO_DIR = Path(__file__).resolve().parents[3] / "shared" / "proto"


@pytest.fixture(scope="session")
def order_pb2(tmp_path_factory: pytest.TempPathFactory) -> ModuleType:
    out = tmp_path_factory.mktemp("generated")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            str(PROTO_DIR / "order_request.proto"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"protoc failed: {result.stderr}")
    sys.path.insert(0, str(out))
    try:
        return importlib.import_module("order_request_pb2")
    finally:
        sys.path.remove(str(out))
