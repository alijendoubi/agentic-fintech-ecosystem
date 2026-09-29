"""BrokerGatewayServicer against a fake Alpaca (httpx.MockTransport): what reaches the broker,
with which credentials, and what never does."""

from __future__ import annotations

import json
import logging
from types import ModuleType
from typing import Any

import grpc
import httpx
import pytest

from broker_gateway.service import BrokerGatewayServicer

from .helpers import (
    API_KEY,
    SECRET,
    AbortError,
    DevSigner,
    FakeAlpaca,
    FakeContext,
    Proof,
    make_authoriser,
    make_forwarder,
    make_proof,
)

ALLOWED_CALLS = {
    ("POST", "/v2/orders"),
    ("DELETE", "/v2/orders/904837e3-3b76-47ec-b432-046db621571b"),
    ("GET", "/v2/orders:by_client_order_id"),
    ("GET", "/v2/orders"),
    ("GET", "/v2/positions"),
    ("GET", "/v2/account"),
}


class Rig:
    def __init__(self, pb2: ModuleType) -> None:
        self.pb2 = pb2
        self.signer = DevSigner()
        self.alpaca = FakeAlpaca()
        self.servicer = BrokerGatewayServicer(
            pb2,
            authoriser=make_authoriser(self.signer),
            forwarder=make_forwarder(self.alpaca),
            allowed_client_cns={"execution-motor"},
        )

    def submit_request(self, proof: Proof, **intent_overrides: Any) -> Any:
        f = proof.fields
        intent = {
            "client_order_id": f["signal_id"],
            "symbol": f["symbol"],
            "side": "buy",
            "order_type": f["order_type"].lower(),
            "qty_nanos": f["qty_nanos"],
            "limit_price_nanos": f["limit_price_nanos"],
            "stop_price_nanos": f["stop_price_nanos"],
            "time_in_force": "day",
        }
        intent.update(intent_overrides)
        return self.pb2.SubmitOrderRequest(
            attestation=self.pb2.AttestationProof(
                canonical_text=proof.text, signature=proof.signature, key_id=proof.key_id
            ),
            intent=self.pb2.OrderIntent(**intent),
        )

    def submit(self, proof: Proof, cn: str | None = "execution-motor", **overrides: Any) -> Any:
        return self.servicer.SubmitOrder(self.submit_request(proof, **overrides), FakeContext(cn))


@pytest.fixture
def rig(pb2: ModuleType) -> Rig:
    return Rig(pb2)


def test_valid_order_is_forwarded_once_with_broker_auth(rig: Rig) -> None:
    reply = rig.submit(make_proof(rig.signer)).reply
    assert reply.refusal == "" and not reply.transport_error
    assert reply.http_status == 200
    assert json.loads(reply.body)["client_order_id"] == make_proof(rig.signer).fields["signal_id"]
    assert reply.base_url == "https://paper-api.alpaca.markets"
    (req,) = rig.alpaca.requests
    assert (req.method, req.url.host, req.url.path) == (
        "POST",
        "paper-api.alpaca.markets",
        "/v2/orders",
    )
    assert req.headers["APCA-API-KEY-ID"] == API_KEY
    assert req.headers["APCA-API-SECRET-KEY"] == SECRET
    assert json.loads(req.content) == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "client_order_id": "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11",
        "limit_price": "150.5",
    }


def test_broker_error_status_and_body_pass_through_unparsed(rig: Rig) -> None:
    rig.alpaca.status = 422
    reply = rig.submit(make_proof(rig.signer)).reply
    assert reply.http_status == 422 and reply.refusal == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_order_id", "other-id"),
        ("symbol", "MSFT"),
        ("side", "sell"),
        ("order_type", "market"),
        ("qty_nanos", 1),
        ("limit_price_nanos", 1),
        ("stop_price_nanos", 1),
        ("time_in_force", "gtc"),
    ],
)
def test_field_mismatch_is_refused_and_nothing_is_sent(rig: Rig, field: str, value: Any) -> None:
    reply = rig.submit(make_proof(rig.signer), **{field: value}).reply
    assert reply.refusal in ("order_mismatch", "time_in_force_not_allowed")
    assert reply.http_status == 0 and reply.body == b""
    assert rig.alpaca.requests == []


def test_replay_is_refused_and_sent_only_once(rig: Rig) -> None:
    proof = make_proof(rig.signer)
    assert rig.submit(proof).reply.http_status == 200
    assert rig.submit(proof).reply.refusal == "replayed"
    assert len(rig.alpaca.requests) == 1


def test_submit_without_attestation_is_refused(rig: Rig, pb2: ModuleType) -> None:
    request = pb2.SubmitOrderRequest(intent=pb2.OrderIntent(client_order_id="x", symbol="AAPL"))
    reply = rig.servicer.SubmitOrder(request, FakeContext()).reply
    assert reply.refusal == "malformed_request"
    assert rig.alpaca.requests == []


def test_cancel_needs_no_attestation(rig: Rig, pb2: ModuleType) -> None:
    oid = "904837e3-3b76-47ec-b432-046db621571b"
    reply = rig.servicer.CancelOrder(pb2.CancelOrderRequest(broker_order_id=oid), FakeContext())
    assert reply.reply.http_status == 204
    (req,) = rig.alpaca.requests
    assert (req.method, req.url.path) == ("DELETE", f"/v2/orders/{oid}")
    assert req.headers["APCA-API-KEY-ID"] == API_KEY


@pytest.mark.parametrize(
    "bad_id", ["", "../account", "a/b", "x?y=1", "a" * 65, "%2e%2e", "id with space"]
)
def test_cancel_id_that_could_reach_another_endpoint_is_refused(
    rig: Rig, pb2: ModuleType, bad_id: str
) -> None:
    reply = rig.servicer.CancelOrder(pb2.CancelOrderRequest(broker_order_id=bad_id), FakeContext())
    assert reply.reply.refusal == "malformed_request"
    assert rig.alpaca.requests == []


@pytest.mark.parametrize("bad_id", ["", "a&status=all", "a" * 65, "x/y"])
def test_order_lookup_id_is_validated(rig: Rig, pb2: ModuleType, bad_id: str) -> None:
    request = pb2.GetOrderByClientOrderIdRequest(client_order_id=bad_id)
    assert rig.servicer.GetOrderByClientOrderId(request, FakeContext()).reply.refusal == (
        "malformed_request"
    )
    assert rig.alpaca.requests == []


def test_every_operation_maps_to_exactly_one_allowlisted_endpoint(
    rig: Rig, pb2: ModuleType
) -> None:
    ctx = FakeContext()
    rig.submit(make_proof(rig.signer))
    rig.servicer.CancelOrder(
        pb2.CancelOrderRequest(broker_order_id="904837e3-3b76-47ec-b432-046db621571b"), ctx
    )
    rig.servicer.GetOrderByClientOrderId(
        pb2.GetOrderByClientOrderIdRequest(client_order_id="abc-1"), ctx
    )
    rig.servicer.ListOpenOrders(pb2.ListOpenOrdersRequest(), ctx)
    rig.servicer.GetPositions(pb2.GetPositionsRequest(), ctx)
    rig.servicer.GetAccount(pb2.GetAccountRequest(), ctx)
    assert set(rig.alpaca.calls()) == ALLOWED_CALLS
    assert len(rig.alpaca.requests) == len(ALLOWED_CALLS)
    listing = next(
        r for r in rig.alpaca.requests if r.url.path == "/v2/orders" and r.method == "GET"
    )
    assert dict(listing.url.params) == {"status": "open", "limit": "500", "direction": "asc"}
    # The service exposes nothing else: no generic proxy method exists.
    rpcs = {name for name in dir(rig.servicer) if name[:1].isupper()}
    assert rpcs == {
        "SubmitOrder",
        "CancelOrder",
        "GetOrderByClientOrderId",
        "ListOpenOrders",
        "GetPositions",
        "GetAccount",
    }


@pytest.mark.parametrize("cn", [None, "cognitive-core", "execution-motor-evil", ""])
def test_peer_not_on_the_allow_list_is_aborted_before_any_work(
    rig: Rig, pb2: ModuleType, cn: str | None
) -> None:
    with pytest.raises(AbortError) as info:
        rig.submit(make_proof(rig.signer), cn=cn)
    assert info.value.code is grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(AbortError):
        rig.servicer.GetAccount(pb2.GetAccountRequest(), FakeContext(cn))
    assert rig.alpaca.requests == []


def test_broker_transport_error_is_reported_as_such(rig: Rig) -> None:
    rig.alpaca.fail_with = httpx.ReadTimeout("read timed out")
    reply = rig.submit(make_proof(rig.signer)).reply
    assert reply.transport_error and reply.http_status == 0 and reply.refusal == ""


def test_credentials_never_appear_in_logs_replies_or_reprs(
    rig: Rig, pb2: ModuleType, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ctx = FakeContext()
    proof = make_proof(rig.signer)
    outputs: list[str] = []
    outputs.append(str(rig.submit(proof)))  # forwarded
    outputs.append(str(rig.submit(proof)))  # replay refusal
    outputs.append(str(rig.submit(make_proof(rig.signer, symbol="MSFT"))))  # mismatch
    rig.alpaca.fail_with = httpx.ConnectError(f"boom {API_KEY} {SECRET}")  # hostile message
    outputs.append(str(rig.servicer.GetAccount(pb2.GetAccountRequest(), ctx)))  # transport error
    outputs.append(
        str(rig.servicer.CancelOrder(pb2.CancelOrderRequest(broker_order_id="../x"), ctx))
    )
    with pytest.raises(AbortError):
        rig.servicer.GetPositions(pb2.GetPositionsRequest(), FakeContext("intruder"))
    outputs.append(repr(rig.servicer._forwarder))  # noqa: SLF001
    captured = capsys.readouterr()
    text = "\n".join([*outputs, captured.out, captured.err, caplog.text])
    assert "gateway_forwarded" in text  # the logs were captured at all
    assert API_KEY not in text
    assert SECRET not in text
