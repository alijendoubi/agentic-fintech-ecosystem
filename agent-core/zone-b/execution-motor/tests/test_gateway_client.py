"""GatewayTransport: AlpacaPaperBroker over the broker gateway's RPCs (ADR-004). The gateway is a
recording fake stub here; the real gateway is exercised end to end in agent-core/tests/e2e."""

from __future__ import annotations

import json
from decimal import Decimal
from types import ModuleType
from typing import Any

import grpc
import pytest

from execution_motor.alpaca import AlpacaPaperBroker
from execution_motor.broker import AttestationProof, BrokerOrderRequest
from execution_motor.config import MotorConfig
from execution_motor.errors import (
    BrokerError,
    BrokerReadError,
    BrokerRejectedError,
    SubmitOutcomeUnknown,
)
from execution_motor.gateway_client import GatewayTransport, _to_nanos, gateway_paper_broker
from execution_motor.halt import KillSwitch
from execution_motor.models import ExecutionStatus, OrderType, RejectReason, Side
from execution_motor.motor import ExecutionMotor
from execution_motor.proto_adapter import attested_order_from_proto
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy
from execution_motor.verifiers import AegisAttestationVerifier, RegisteredKey, SignatureAlgorithm

from .test_aegis_fixtures import load_fixture, to_messages
from .test_alpaca_http import CID, order_json

PAPER = "https://paper-api.alpaca.markets"
PROOF = AttestationProof(canonical_text=b"afe-attest-v2\n...", signature=b"s" * 64, key_id="k1")


class FakeRpcError(grpc.RpcError):
    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE


class FakeGatewayStub:
    """Records every RPC; answers from a per-RPC script (default: 200 + an order body)."""

    def __init__(self, pb2: ModuleType, **script: Any) -> None:
        self.pb2 = pb2
        self.script = script
        self.calls: list[tuple[str, Any, float | None]] = []

    def __getattr__(self, rpc: str) -> Any:
        if not rpc[:1].isupper():
            raise AttributeError(rpc)

        def call(message: Any, timeout: float | None = None) -> Any:
            self.calls.append((rpc, message, timeout))
            outcome = self.script.get(rpc, {"http_status": 200, "body": order_json()})
            if isinstance(outcome, Exception):
                raise outcome
            fields = dict(outcome)
            if "body" in fields and not isinstance(fields["body"], bytes):
                fields["body"] = json.dumps(fields["body"]).encode()
            fields.setdefault("base_url", PAPER)
            return getattr(self.pb2, f"{rpc}Response")(reply=self.pb2.BrokerReply(**fields))

        return call

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def req(**overrides: Any) -> BrokerOrderRequest:
    fields: dict[str, Any] = {
        "client_order_id": CID,
        "symbol": "AAPL",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": Decimal("10"),
        "limit_price": Decimal("190.50"),
        "attestation": PROOF,
    }
    fields.update(overrides)
    return BrokerOrderRequest(**fields)


def broker(stub: FakeGatewayStub) -> AlpacaPaperBroker:
    return gateway_paper_broker(stub, stub.pb2, sleep=lambda _s: None, rng=lambda: 0.0)


@pytest.fixture
def gw(pb2: dict[str, ModuleType]) -> ModuleType:
    return pb2["gateway"]


# ------------------------------------------------------------------------- submit


def test_submit_sends_the_proof_and_the_exact_intent(gw: ModuleType) -> None:
    stub = FakeGatewayStub(gw)
    result = broker(stub).submit_order(req())
    assert result.status is ExecutionStatus.ACCEPTED
    ((rpc, msg, timeout),) = stub.calls
    assert rpc == "SubmitOrder" and timeout == 7.0
    assert msg.attestation.canonical_text == PROOF.canonical_text
    assert msg.attestation.signature == PROOF.signature
    assert msg.attestation.key_id == "k1"
    assert (msg.intent.client_order_id, msg.intent.symbol, msg.intent.side) == (CID, "AAPL", "buy")
    assert msg.intent.order_type == "limit" and msg.intent.time_in_force == "day"
    assert msg.intent.qty_nanos == 10 * 10**9
    assert msg.intent.limit_price_nanos == 190_500_000_000
    assert msg.intent.stop_price_nanos == 0


def test_submit_without_attestation_is_refused_locally(gw: ModuleType) -> None:
    stub = FakeGatewayStub(gw)
    with pytest.raises(BrokerRejectedError, match="attestation_missing"):
        broker(stub).submit_order(req(attestation=None))
    assert stub.calls == []


def test_gateway_refusal_is_a_definitive_reject(gw: ModuleType) -> None:
    stub = FakeGatewayStub(gw, SubmitOrder={"refusal": "replayed"})
    with pytest.raises(BrokerRejectedError, match="replayed"):
        broker(stub).submit_order(req())
    assert stub.names() == ["SubmitOrder"]  # no reconciliation: nothing was sent


def test_broker_4xx_passes_through_as_a_reject(gw: ModuleType) -> None:
    stub = FakeGatewayStub(
        gw, SubmitOrder={"http_status": 422, "body": {"message": "insufficient buying power"}}
    )
    with pytest.raises(BrokerRejectedError, match="insufficient buying power"):
        broker(stub).submit_order(req())


@pytest.mark.parametrize(
    "outcome",
    [{"transport_error": True}, FakeRpcError(), {"http_status": 200, "base_url": "https://x"}],
)
def test_ambiguous_submit_is_reconciled_by_client_order_id(gw: ModuleType, outcome: Any) -> None:
    stub = FakeGatewayStub(gw, SubmitOrder=outcome)
    result = broker(stub).submit_order(req())
    assert result.client_order_id == CID
    assert stub.names() == ["SubmitOrder", "GetOrderByClientOrderId"]
    assert stub.calls[1][1].client_order_id == CID


def test_ambiguous_submit_with_failed_reconciliation_is_unknown(gw: ModuleType) -> None:
    stub = FakeGatewayStub(
        gw, SubmitOrder={"transport_error": True}, GetOrderByClientOrderId=FakeRpcError()
    )
    with pytest.raises(SubmitOutcomeUnknown):
        broker(stub).submit_order(req())


# -------------------------------------------------------------------- other calls


def test_reads_and_cancel_map_to_their_rpcs(gw: ModuleType) -> None:
    stub = FakeGatewayStub(
        gw,
        ListOpenOrders={"http_status": 200, "body": [order_json()]},
        GetPositions={"http_status": 200, "body": []},
        GetAccount={
            "http_status": 200,
            "body": {
                "status": "ACTIVE",
                "buying_power": "1",
                "cash": "1",
                "equity": "100000",
                "trading_blocked": False,
                "account_blocked": False,
            },
        },
        CancelOrder={"http_status": 204, "body": b""},
    )
    b = broker(stub)
    assert len(b.list_open_orders()) == 1
    assert b.get_positions() == ()
    assert b.get_account().equity == Decimal("100000")
    b.cancel_order("904837e3-3b76-47ec-b432-046db621571b")
    assert stub.names() == ["ListOpenOrders", "GetPositions", "GetAccount", "CancelOrder"]
    assert stub.calls[3][1].broker_order_id == "904837e3-3b76-47ec-b432-046db621571b"


def test_refused_read_and_cancel_fail_closed(gw: ModuleType) -> None:
    stub = FakeGatewayStub(
        gw,
        GetAccount={"refusal": "malformed_request"},
        CancelOrder={"refusal": "malformed_request"},
    )
    with pytest.raises(BrokerReadError):
        broker(stub).get_account()
    with pytest.raises(BrokerError):
        broker(stub).cancel_order("abc")


def test_unmapped_request_never_reaches_the_gateway(gw: ModuleType) -> None:
    import httpx

    stub = FakeGatewayStub(gw)
    client = httpx.Client(base_url=PAPER, transport=GatewayTransport(stub, gw))
    for method, path in (
        ("GET", "/v2/assets"),
        ("POST", "/v2/positions"),
        ("DELETE", "/v2/positions"),
        ("GET", "/v2/orders?status=all"),
        ("PATCH", "/v2/orders/abc"),
    ):
        with pytest.raises(httpx.UnsupportedProtocol):
            client.request(method, path)
    assert stub.calls == []


@pytest.mark.parametrize(
    ("text", "nanos"), [("10", 10**10), ("0.000000001", 1), ("190.50", 190_500_000_000)]
)
def test_decimal_strings_convert_to_exact_nanos(text: str, nanos: int) -> None:
    assert _to_nanos(text, "x") == nanos


@pytest.mark.parametrize("text", ["0.0000000001", "-1", "NaN", "abc", "Infinity"])
def test_non_representable_quantities_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        _to_nanos(text, "x")


# ------------------------------------------------- the motor hands the proof over


def _motor_and_decision(
    pb2: dict[str, ModuleType], stub: FakeGatewayStub
) -> tuple[ExecutionMotor, Any, dict[str, Any]]:
    fx = load_fixture()
    order, att = to_messages(pb2, fx["cases"]["buy"])
    key = RegisteredKey(
        fx["key_id"], SignatureAlgorithm.ED25519_DEV, bytes.fromhex(fx["public_key_hex"])
    )
    motor = ExecutionMotor(
        config=MotorConfig(Decimal(25_000), Decimal(100_000), environment="development"),
        brokers={"alpaca-paper": broker(stub)},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=KillSwitch(start_halted=False),
        verifier=AegisAttestationVerifier([key], production=False),
        clock_ns=lambda: fx["now_ns"] + 1,
    )
    return motor, attested_order_from_proto(order, att), fx


def test_motor_attaches_the_verified_attestation_to_the_broker_request(
    pb2: dict[str, ModuleType],
) -> None:
    stub = FakeGatewayStub(pb2["gateway"])
    motor, attested, fx = _motor_and_decision(pb2, stub)
    case = fx["cases"]["buy"]
    stub.script["SubmitOrder"] = {
        "http_status": 200,
        "body": order_json(client_order_id=attested.order.order_id),
    }
    report = motor.execute(attested)
    assert report.status is ExecutionStatus.ACCEPTED, report
    ((_, msg, _),) = stub.calls
    assert msg.attestation.canonical_text == case["text"].encode()
    assert msg.attestation.signature == bytes.fromhex(case["attestation"]["signature_hex"])
    assert msg.attestation.key_id == fx["key_id"]
    assert msg.intent.qty_nanos == case["order"]["quantity_nanos"]
    assert msg.intent.limit_price_nanos == case["order"]["limit_price_nanos"]


def test_a_gateway_refusal_is_reported_as_broker_rejected(pb2: dict[str, ModuleType]) -> None:
    stub = FakeGatewayStub(pb2["gateway"], SubmitOrder={"refusal": "signature_invalid"})
    motor, attested, _ = _motor_and_decision(pb2, stub)
    report = motor.execute(attested)
    assert report.status is ExecutionStatus.REJECTED
    assert report.reject_reason is RejectReason.BROKER_REJECTED
    assert "signature_invalid" in report.reject_detail
