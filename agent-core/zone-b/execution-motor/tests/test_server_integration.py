"""Real grpc.Server on loopback with ephemeral mTLS certs (no files checked in; certs are
generated per test run by tests/tls_certs.py). Proves:

1. mTLS is actually enforced by the wire, not just by the config parser: a client with no
   certificate never reaches a handler.
2. A tampered AegisDecision (bad signature on an otherwise real, Aegis-signed fixture) is
   rejected by the real pipeline (attestation verification) before it ever reaches the
   broker, even over a real authenticated (client-certificate-bearing) connection.
3. A genuine, untampered, Aegis-signed decision succeeds end to end over the real network
   path (positive control for 1 and 2: proves the server is not simply refusing everything).

Uses the real ``aegis_attestations.json`` fixture (Ed25519 signatures produced by the
actual Aegis Rust crate, see tests/fixtures/gen_aegis_fixtures.sh) so this exercises the
genuine dual-verifier, not a test double.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from collections.abc import Iterator
from concurrent import futures
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import grpc
import pytest

from execution_motor.config import MotorConfig
from execution_motor.grpc_service import ExecutionMotorServicer
from execution_motor.halt import KillSwitch
from execution_motor.mock_broker import MockBroker
from execution_motor.motor import ExecutionMotor
from execution_motor.server import build_server_credentials
from execution_motor.server_config import TlsPaths
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy

from .conftest import PROTO_DIR
from .test_aegis_fixtures import load_fixture, to_messages
from .test_verifiers import dev_verifier
from .tls_certs import PkiBundle, build_pki, write_pem

# decided_at_ns / expires_at_ns baked into the "buy" fixture case (see
# tests/fixtures/aegis_attestations.json); the servicer's injected clock is pinned to a
# moment inside that fixture's [decided_at_ns, expires_at_ns) window.
_FIXED_NOW_NS = 1_790_000_000_500_000_000


@pytest.fixture(scope="module")
def grpc_pb2(tmp_path_factory: pytest.TempPathFactory) -> dict[str, ModuleType]:
    """Full message + gRPC Python stubs (unlike the session-scoped ``pb2`` fixture in
    conftest.py, which only compiles messages, not service stubs)."""
    out = tmp_path_factory.mktemp("grpc_generated")
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
    try:
        return {
            "aegis": importlib.import_module("aegis_pb2"),
            "order": importlib.import_module("order_request_pb2"),
            "motor": importlib.import_module("execution_motor_pb2"),
            "motor_grpc": importlib.import_module("execution_motor_pb2_grpc"),
        }
    finally:
        sys.path.remove(str(out))


def _decision_message(grpc_pb2: dict[str, ModuleType], *, tamper: bool) -> object:
    case = load_fixture()["cases"]["buy"]
    order, att = to_messages(grpc_pb2, case)
    if tamper:
        bad = b"\x00" * len(att.signature)
        att.signature = bad
        order.hsm_signature = bad
    return grpc_pb2["aegis"].AegisDecision(
        signal_id=case["order"]["signal_id"],
        decision=1,  # DECISION_APPROVED
        attestation=att,
        order=order,
        decided_at_ns=_FIXED_NOW_NS,
    )


def _build_motor(broker: MockBroker) -> ExecutionMotor:
    config = MotorConfig(
        max_order_notional=Decimal("100000"),
        max_session_notional=Decimal("1000000"),
        environment="test",
    )
    return ExecutionMotor(
        config=config,
        brokers={broker.venue: broker},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=KillSwitch(start_halted=False),
        verifier=dev_verifier(),
        clock_ns=lambda: _FIXED_NOW_NS,
    )


class RunningServer:
    def __init__(self, port: int, pki: PkiBundle, broker: MockBroker) -> None:
        self.port = port
        self.pki = pki
        self.broker = broker

    def channel_with_client_cert(self) -> grpc.Channel:
        creds = grpc.ssl_channel_credentials(
            root_certificates=self.pki.ca.cert_pem,
            certificate_chain=self.pki.client.cert_pem,
            private_key=self.pki.client.key_pem,
        )
        return grpc.secure_channel(f"127.0.0.1:{self.port}", creds)

    def channel_without_client_cert(self) -> grpc.Channel:
        creds = grpc.ssl_channel_credentials(root_certificates=self.pki.ca.cert_pem)
        return grpc.secure_channel(f"127.0.0.1:{self.port}", creds)


@pytest.fixture
def running_server(grpc_pb2: dict[str, ModuleType], tmp_path: Path) -> Iterator[RunningServer]:
    pki = build_pki()
    server_cert = write_pem(tmp_path, "server.pem", pki.server.cert_pem)
    server_key = write_pem(tmp_path, "server.key", pki.server.key_pem)
    ca_cert = write_pem(tmp_path, "ca.pem", pki.ca.cert_pem)
    creds = build_server_credentials(TlsPaths(cert=server_cert, key=server_key, client_ca=ca_cert))

    broker = MockBroker()
    motor = _build_motor(broker)
    servicer = ExecutionMotorServicer(
        motor,
        grpc_pb2["motor"],
        kill_switch=KillSwitch(start_halted=False),
        clock_ns=lambda: _FIXED_NOW_NS,
    )
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    grpc_pb2["motor_grpc"].add_ExecutionMotorServicer_to_server(servicer, server)
    port = server.add_secure_port("127.0.0.1:0", creds)
    assert port != 0, "server refused to bind (bad TLS material?)"
    server.start()
    try:
        yield RunningServer(port=port, pki=pki, broker=broker)
    finally:
        server.stop(grace=0).wait(timeout=5)


def test_client_without_certificate_is_rejected(running_server: RunningServer) -> None:
    channel = running_server.channel_without_client_cert()
    try:
        with pytest.raises(grpc.FutureTimeoutError):
            # require_client_auth=True means the TLS handshake itself never completes for a
            # peer that presents no certificate: the channel never becomes READY.
            grpc.channel_ready_future(channel).result(timeout=3)
    finally:
        channel.close()


def test_tampered_attestation_never_reaches_broker_over_real_mtls(
    running_server: RunningServer, grpc_pb2: dict[str, ModuleType]
) -> None:
    channel = running_server.channel_with_client_cert()
    try:
        stub = grpc_pb2["motor_grpc"].ExecutionMotorStub(channel)
        ack = stub.Execute(_decision_message(grpc_pb2, tamper=True), timeout=5)
        assert ack.accepted is False
        assert ack.reject_reason == "attestation_invalid"
        order_id = load_fixture()["cases"]["buy"]["order"]["order_id"]
        assert running_server.broker.get_order_by_client_id(order_id) is None
    finally:
        channel.close()


def test_genuine_aegis_signed_decision_succeeds_over_real_mtls(
    running_server: RunningServer, grpc_pb2: dict[str, ModuleType]
) -> None:
    channel = running_server.channel_with_client_cert()
    try:
        stub = grpc_pb2["motor_grpc"].ExecutionMotorStub(channel)
        ack = stub.Execute(_decision_message(grpc_pb2, tamper=False), timeout=5)
        assert ack.accepted is True
        order_id = load_fixture()["cases"]["buy"]["order"]["order_id"]
        assert running_server.broker.get_order_by_client_id(order_id) is not None

        health = stub.Health(grpc_pb2["aegis"].Empty(), timeout=5)
        assert health.ok is True
        assert health.halted is False
    finally:
        channel.close()
