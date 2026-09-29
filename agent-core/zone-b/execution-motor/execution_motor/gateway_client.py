"""Broker access through the broker gateway (ADR-004 Option C). The motor holds no broker
credentials: ``AlpacaPaperBroker`` keeps all Alpaca request/response logic and this module is
the ``httpx`` transport underneath it, turning each of its six calls into one
``BrokerGatewayService`` RPC (agent-core/shared/proto/broker_gateway.proto).

Mapping (anything else raises before any RPC is made):

    POST   /v2/orders                          -> SubmitOrder (with the order's attestation)
    GET    /v2/orders:by_client_order_id       -> GetOrderByClientOrderId
    GET    /v2/orders?status=open&...          -> ListOpenOrders
    DELETE /v2/orders/{id}                     -> CancelOrder
    GET    /v2/positions                       -> GetPositions
    GET    /v2/account                         -> GetAccount

Outcomes, mapped so the existing ``AlpacaBroker`` semantics hold unchanged:

* gateway refusal (nothing was sent) -> HTTP 403 with the reason in ``message``; for a submit
  ``AlpacaBroker`` raises ``BrokerRejectedError`` (definitive: the order does not exist).
* gateway ``transport_error``, any gRPC error, or a reply for a different broker endpoint
  than this client's -> ``httpx.TransportError``; a submit is then reconciled by client order
  id and, if that fails too, reported as ``SubmitOutcomeUnknown`` (the motor halts).
* otherwise the broker's own status and body, unparsed by the gateway.

A submit without an attestation is refused locally with the same 403 shape and no RPC.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import grpc
import httpx
import structlog

from .alpaca import ATTESTATION_EXTENSION, PAPER_BASE_URL, AlpacaPaperBroker
from .broker import AttestationProof

_log = structlog.get_logger("execution_motor.gateway_client")
_NANOS: Final = Decimal(1_000_000_000)
_OPEN_ORDERS_PARAMS: Final = {"status": "open", "limit": "500", "direction": "asc"}
# Must exceed the gateway's own broker timeout (GATEWAY_BROKER_TIMEOUT_MS, default 5 s) so a
# slow broker surfaces as the gateway's transport_error rather than a motor-side deadline.
# The 2 s margin is an engineering default, not a measurement.
DEFAULT_DEADLINE_S: Final = 7.0
REFUSAL_STATUS: Final = 403


def _to_nanos(value: Any, name: str) -> int:
    """Exact decimal string -> int nanos; refuses anything that is not a whole nano."""
    if value is None:
        return 0
    try:
        scaled = Decimal(str(value)) * _NANOS
    except InvalidOperation as exc:
        raise ValueError(f"{name} is not a decimal") from exc
    if not scaled.is_finite() or scaled != scaled.to_integral_value() or scaled < 0:
        raise ValueError(f"{name} is not a non-negative whole number of nanos")
    return int(scaled)


class GatewayTransport(httpx.BaseTransport):
    def __init__(
        self,
        stub: Any,
        pb2: Any,
        *,
        expected_base_url: str = PAPER_BASE_URL,
        deadline_s: float = DEFAULT_DEADLINE_S,
    ) -> None:
        self._stub = stub
        self._pb2 = pb2
        self._expected_base_url = expected_base_url
        self._deadline = deadline_s

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        params = dict(request.url.params)
        if method == "POST" and path == "/v2/orders":
            return self._submit(request)
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            msg = self._pb2.GetOrderByClientOrderIdRequest(
                client_order_id=params.get("client_order_id", "")
            )
            return self._call("GetOrderByClientOrderId", msg, request)
        if method == "GET" and path == "/v2/orders" and params == _OPEN_ORDERS_PARAMS:
            return self._call("ListOpenOrders", self._pb2.ListOpenOrdersRequest(), request)
        if method == "DELETE" and path.startswith("/v2/orders/") and not params:
            msg = self._pb2.CancelOrderRequest(broker_order_id=path.removeprefix("/v2/orders/"))
            return self._call("CancelOrder", msg, request)
        if method == "GET" and path == "/v2/positions" and not params:
            return self._call("GetPositions", self._pb2.GetPositionsRequest(), request)
        if method == "GET" and path == "/v2/account" and not params:
            return self._call("GetAccount", self._pb2.GetAccountRequest(), request)
        # Programming error: AlpacaBroker only makes the calls above. Nothing is sent.
        raise httpx.UnsupportedProtocol(
            f"no gateway operation for {method} {path}", request=request
        )

    def _submit(self, request: httpx.Request) -> httpx.Response:
        proof = request.extensions.get(ATTESTATION_EXTENSION)
        if not isinstance(proof, AttestationProof):
            return _refusal(request, "attestation_missing")
        try:
            body = json.loads(request.content)
            intent = self._pb2.OrderIntent(
                client_order_id=str(body["client_order_id"]),
                symbol=str(body["symbol"]),
                side=str(body["side"]),
                order_type=str(body["type"]),
                qty_nanos=_to_nanos(body["qty"], "qty"),
                limit_price_nanos=_to_nanos(body.get("limit_price"), "limit_price"),
                stop_price_nanos=_to_nanos(body.get("stop_price"), "stop_price"),
                time_in_force=str(body["time_in_force"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            return _refusal(request, f"unencodable_order:{type(exc).__name__}")
        msg = self._pb2.SubmitOrderRequest(
            attestation=self._pb2.AttestationProof(
                canonical_text=proof.canonical_text,
                signature=proof.signature,
                key_id=proof.key_id,
            ),
            intent=intent,
        )
        return self._call("SubmitOrder", msg, request)

    def _call(self, rpc: str, message: Any, request: httpx.Request) -> httpx.Response:
        try:
            reply = getattr(self._stub, rpc)(message, timeout=self._deadline).reply
        except grpc.RpcError as exc:
            code = exc.code() if callable(getattr(exc, "code", None)) else None
            raise httpx.ConnectError(
                f"broker gateway {rpc} failed: {code}", request=request
            ) from None
        if reply.base_url != self._expected_base_url:
            _log.critical(
                "broker_gateway_endpoint_mismatch",
                rpc=rpc,
                expected=self._expected_base_url,
                reported=reply.base_url,
            )
            raise httpx.RemoteProtocolError("broker gateway endpoint mismatch", request=request)
        if reply.refusal:
            _log.warning(
                "broker_gateway_refused", rpc=rpc, reason=reply.refusal, detail=reply.detail
            )
            return _refusal(request, reply.refusal, reply.detail)
        if reply.transport_error:
            raise httpx.ReadError(f"broker gateway {rpc}: broker transport error", request=request)
        return httpx.Response(reply.http_status, content=bytes(reply.body), request=request)


def _refusal(request: httpx.Request, reason: str, detail: str = "") -> httpx.Response:
    message = f"broker gateway refused: {reason}" + (f" ({detail})" if detail else "")
    return httpx.Response(
        REFUSAL_STATUS,
        json={"message": message},
        headers={"x-afe-gateway-refusal": reason},
        request=request,
    )


def open_gateway_channel(target: str, ca: Path, cert: Path, key: Path) -> grpc.Channel:
    """Mutual TLS only: the gateway refuses clients without a certificate."""
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca.read_bytes(),
        certificate_chain=cert.read_bytes(),
        private_key=key.read_bytes(),
    )
    return grpc.secure_channel(target, credentials)


def gateway_paper_broker(stub: Any, pb2: Any, **broker_kwargs: Any) -> AlpacaPaperBroker:
    """The production broker: Alpaca paper, reached only through the gateway."""
    return AlpacaPaperBroker(transport=GatewayTransport(stub, pb2), **broker_kwargs)
