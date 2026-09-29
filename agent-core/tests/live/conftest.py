"""Fixtures for the live-stack tests (ALI-166).

These tests talk to a RUNNING dev compose stack over its real network paths and real
mTLS, never to fakes. They are skipped unless ``AFE_LIVE_STACK=1``. See README.md.

Timestamps come from the stack's own clock (Redis ``TIME``, inside the Docker VM),
not the host clock: on Docker Desktop the two drift (0.76 s observed), which is a
large fraction of Aegis's 1 s reference-data freshness window.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import grpc
import pytest

AGENT_CORE = Path(__file__).resolve().parents[2]
PROTO_DIR = AGENT_CORE / "shared" / "proto"
DEV_TLS = AGENT_CORE / "infrastructure" / "dev-tls" / "out"

DEV_ENV_FILE = AGENT_CORE / "infrastructure" / ".env"

AEGIS_ADDR = os.environ.get("AFE_LIVE_AEGIS", "127.0.0.1:50051")
MOTOR_ADDR = os.environ.get("AFE_LIVE_MOTOR", "127.0.0.1:50052")
REDIS_HOST_PORT = os.environ.get("AFE_LIVE_REDIS", "127.0.0.1:6379")
HITL_URL = os.environ.get("AFE_LIVE_HITL", "http://127.0.0.1:3000")

# Redis ACL users (ALI-20) and the .env key holding each one's password. Each test publishes
# as the service that owns the channel, exactly as the real producers do.
REDIS_USERS = {
    "sensory-array": "REDIS_SENSORY_PASSWORD",
    "regime-detector": "REDIS_REGIME_PASSWORD",
    "healthcheck": "REDIS_HEALTHCHECK_PASSWORD",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("AFE_LIVE_STACK") == "1":
        return
    skip = pytest.mark.skip(reason="live-stack tests: set AFE_LIVE_STACK=1 with the dev stack up")
    for item in items:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def pb(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType]:
    """Compile every proto into a temp dir (nothing is written to the repo)."""
    out = tmp_path_factory.mktemp("live_generated")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            f"--grpc_python_out={out}",
            *(str(f) for f in sorted(PROTO_DIR.glob("*.proto"))),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"protoc failed: {result.stderr}")
    sys.path.insert(0, str(out))
    names = (
        "aegis_pb2",
        "aegis_pb2_grpc",
        "trade_signal_pb2",
        "market_snapshot_pb2",
        "execution_motor_pb2",
        "execution_motor_pb2_grpc",
    )
    return {n: importlib.import_module(n) for n in names}


def _read(path: Path) -> bytes:
    if not path.is_file():
        pytest.fail(f"missing dev TLS file {path}: run dev-tls/generate-dev-certs.sh first")
    return path.read_bytes()


def mtls_channel(addr: str, identity: str, server_name: str) -> grpc.Channel:
    """Client channel presenting the dev-tls client certificate of ``identity``."""
    d = DEV_TLS / identity
    creds = grpc.ssl_channel_credentials(
        root_certificates=_read(d / "ca.pem"),
        private_key=_read(d / "client.key"),
        certificate_chain=_read(d / "client.pem"),
    )
    return grpc.secure_channel(
        addr, creds, options=(("grpc.ssl_target_name_override", server_name),)
    )


def ca_only_channel(addr: str, server_name: str) -> grpc.Channel:
    """TLS channel that trusts the dev CA but presents NO client certificate."""
    creds = grpc.ssl_channel_credentials(root_certificates=_read(DEV_TLS / "aegis" / "ca.pem"))
    return grpc.secure_channel(
        addr, creds, options=(("grpc.ssl_target_name_override", server_name),)
    )


@pytest.fixture(scope="session")
def aegis_as(pb: dict[str, ModuleType]) -> Iterator[object]:
    channels: list[grpc.Channel] = []

    def make(identity: str) -> object:
        ch = mtls_channel(AEGIS_ADDR, identity, "aegis")
        channels.append(ch)
        return pb["aegis_pb2_grpc"].AegisStub(ch)

    yield make
    for ch in channels:
        ch.close()


@pytest.fixture(scope="session")
def motor(pb: dict[str, ModuleType]) -> Iterator[object]:
    ch = mtls_channel(MOTOR_ADDR, "cognitive-core", "execution-motor")
    yield pb["execution_motor_pb2_grpc"].ExecutionMotorStub(ch)
    ch.close()


def _dev_env() -> dict[str, str]:
    """KEY=VALUE pairs from the dev stack's infrastructure/.env (never committed)."""
    if not DEV_ENV_FILE.is_file():
        return {}
    pairs = {}
    for line in DEV_ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip()
    return pairs


def redis_password(user: str) -> str:
    key = REDIS_USERS[user]
    value = os.environ.get(key) or _dev_env().get(key, "")
    if not value:
        pytest.fail(f"{key} is not set (environment or {DEV_ENV_FILE})")
    return value


def redis_as(user: str | None, password: str | None = None) -> object:
    """A client authenticated as ``user`` (None: anonymous)."""
    import redis

    host, _, port = REDIS_HOST_PORT.rpartition(":")
    if user is None:
        return redis.Redis(host=host, port=int(port), socket_timeout=5)
    secret = redis_password(user) if password is None else password
    return redis.Redis(host=host, port=int(port), username=user, password=secret, socket_timeout=5)


@pytest.fixture(scope="session")
def snapshot_redis() -> Iterator[object]:
    client = redis_as("sensory-array")
    yield client
    client.close()  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def regime_redis() -> Iterator[object]:
    client = redis_as("regime-detector")
    yield client
    client.close()  # type: ignore[attr-defined]


@pytest.fixture
def stack_now_ns() -> int:
    client = redis_as("healthcheck")
    try:
        seconds, micros = client.time()  # type: ignore[attr-defined]
    finally:
        client.close()  # type: ignore[attr-defined]
    return int(seconds) * 1_000_000_000 + int(micros) * 1_000
