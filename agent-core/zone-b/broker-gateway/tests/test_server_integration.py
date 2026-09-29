"""Real gRPC server on loopback with mutual TLS (ephemeral certificates). The broker behind it
is still the in-memory fake Alpaca: no traffic leaves the machine."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import grpc
import pytest

from broker_gateway.__main__ import GatewayApp, build_server
from broker_gateway.config import GatewayConfig
from broker_gateway.service import BrokerGatewayServicer

from .helpers import (
    API_KEY,
    MOTOR_DIR,
    SECRET,
    DevSigner,
    FakeAlpaca,
    make_authoriser,
    make_forwarder,
    make_proof,
)


def _load_motor_tls_helper() -> ModuleType:
    """Reuse execution-motor's ephemeral-PKI test helper instead of copying it."""
    path = MOTOR_DIR / "tests" / "tls_certs.py"
    spec = importlib.util.spec_from_file_location("motor_tls_certs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["motor_tls_certs"] = module
    spec.loader.exec_module(module)
    return module


tls = _load_motor_tls_helper()


class Server:
    def __init__(self, protos: tuple[ModuleType, ModuleType], tmp: Path) -> None:
        pb2, pb2_grpc = protos
        ca_pem, ca_cert, ca_key = tls.make_ca()
        server_pem = tls.make_leaf("broker-gateway", ca_cert, ca_key, server=True)
        self.ca = ca_pem.cert_pem
        self.clients = {
            cn: tls.make_leaf(cn, ca_cert, ca_key, server=False)
            for cn in ("execution-motor", "cognitive-core")
        }
        other_ca, other_cert, other_key = tls.make_ca("other-ca")
        self.clients["foreign-ca"] = tls.make_leaf(
            "execution-motor", other_cert, other_key, server=False
        )
        config = GatewayConfig.from_env(
            {
                "GATEWAY_ENV": "test",
                "GATEWAY_TLS_CERT": str(tls.write_pem(tmp, "server.pem", server_pem.cert_pem)),
                "GATEWAY_TLS_KEY": str(tls.write_pem(tmp, "server.key", server_pem.key_pem)),
                "GATEWAY_TLS_CLIENT_CA": str(tls.write_pem(tmp, "ca.pem", self.ca)),
                "GATEWAY_ALLOWED_CLIENT_CNS": "execution-motor",
                "GATEWAY_ATTESTATION_KEYS_FILE": str(tmp / "unused.json"),
                "ALPACA_API_KEY": API_KEY,
                "ALPACA_SECRET_KEY": SECRET,
            }
        )
        self.pb2, self.pb2_grpc = pb2, pb2_grpc
        self.signer = DevSigner()
        self.alpaca = FakeAlpaca()
        servicer = BrokerGatewayServicer(
            pb2,
            authoriser=make_authoriser(self.signer),
            forwarder=make_forwarder(self.alpaca),
            allowed_client_cns=config.allowed_client_cns,
        )
        app = GatewayApp(config=config, servicer=servicer, pb2_grpc=pb2_grpc)
        self.server, self.port = build_server(app, listen_addr="127.0.0.1:0")
        self.server.start()

    def stub(self, client: str | None) -> Any:
        pem = self.clients.get(client) if client else None
        creds = grpc.ssl_channel_credentials(
            root_certificates=self.ca,
            private_key=pem.key_pem if pem else None,
            certificate_chain=pem.cert_pem if pem else None,
        )
        channel = grpc.secure_channel(f"localhost:{self.port}", creds)
        return self.pb2_grpc.BrokerGatewayServiceStub(channel)


@pytest.fixture
def server(protos: tuple[ModuleType, ModuleType], tmp_path: Path) -> Iterator[Server]:
    srv = Server(protos, tmp_path)
    yield srv
    srv.server.stop(grace=None)


def test_allowed_client_submits_over_mtls(server: Server) -> None:
    proof = make_proof(server.signer)
    f = proof.fields
    request = server.pb2.SubmitOrderRequest(
        attestation=server.pb2.AttestationProof(
            canonical_text=proof.text, signature=proof.signature, key_id=proof.key_id
        ),
        intent=server.pb2.OrderIntent(
            client_order_id=f["signal_id"],
            symbol=f["symbol"],
            side="buy",
            order_type="limit",
            qty_nanos=f["qty_nanos"],
            limit_price_nanos=f["limit_price_nanos"],
            time_in_force="day",
        ),
    )
    reply = server.stub("execution-motor").SubmitOrder(request, timeout=5).reply
    assert reply.http_status == 200
    assert server.alpaca.calls() == [("POST", "/v2/orders")]


def test_client_cn_not_on_the_allow_list_is_permission_denied(server: Server) -> None:
    with pytest.raises(grpc.RpcError) as info:
        server.stub("cognitive-core").GetAccount(server.pb2.GetAccountRequest(), timeout=5)
    assert info.value.code() is grpc.StatusCode.PERMISSION_DENIED
    assert server.alpaca.requests == []


@pytest.mark.parametrize("client", [None, "foreign-ca"])
def test_no_or_foreign_client_certificate_never_reaches_a_handler(
    server: Server, client: str | None
) -> None:
    with pytest.raises(grpc.RpcError) as info:
        server.stub(client).GetAccount(server.pb2.GetAccountRequest(), timeout=5)
    assert info.value.code() is grpc.StatusCode.UNAVAILABLE
    assert server.alpaca.requests == []


def test_a_method_outside_the_service_is_unimplemented(server: Server) -> None:
    pem = server.clients["execution-motor"]
    channel = grpc.secure_channel(
        f"localhost:{server.port}",
        grpc.ssl_channel_credentials(server.ca, pem.key_pem, pem.cert_pem),
    )
    forward_anything = channel.unary_unary(
        "/afe.shared.BrokerGatewayService/Forward",
        request_serializer=lambda b: b,
        response_deserializer=lambda b: b,
    )
    with pytest.raises(grpc.RpcError) as info:
        forward_anything(b"", timeout=5)
    assert info.value.code() is grpc.StatusCode.UNIMPLEMENTED
    assert server.alpaca.requests == []
