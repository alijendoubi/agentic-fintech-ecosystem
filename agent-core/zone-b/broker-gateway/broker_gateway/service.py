"""``BrokerGatewayService`` servicer: peer check -> (submit only) authorisation -> forward.

Every RPC first checks that the mTLS peer's certificate CN is on the allow-list; anything
else is aborted with PERMISSION_DENIED before any work. After that, every outcome is a normal
``BrokerReply`` (see broker_gateway.proto): ``refusal`` when nothing was sent,
``transport_error`` when the broker gave no HTTP answer, otherwise the broker's status and
body passed through unparsed.

Logging: one structured event per RPC with the operation, the order identifiers, the outcome
and the broker HTTP status. Never the credentials, the request headers or the response body.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

import grpc
import structlog

from .alpaca import AlpacaForwarder, BrokerTransportError, HttpReply, InvalidIdentifier
from .authorize import Intent, Refusal, Refused, SubmitAuthoriser

_log = structlog.get_logger("broker_gateway.service")
_MAX_DETAIL = 200


def peer_common_names(context: Any) -> tuple[str, ...]:
    """CNs of the verified client certificate (gRPC's ``x509_common_name`` auth property)."""
    try:
        auth = context.auth_context()
    except Exception:  # noqa: BLE001 - no auth context means no identity (fail closed)
        return ()
    names = auth.get("x509_common_name", ()) if isinstance(auth, dict) else ()
    return tuple(n.decode("utf-8", "replace") if isinstance(n, bytes) else str(n) for n in names)


class BrokerGatewayServicer:
    def __init__(
        self,
        pb2: Any,
        *,
        authoriser: SubmitAuthoriser,
        forwarder: AlpacaForwarder,
        allowed_client_cns: Collection[str],
    ) -> None:
        if not allowed_client_cns:
            raise ValueError("allowed_client_cns must not be empty")
        self._pb2 = pb2
        self._authoriser = authoriser
        self._forwarder = forwarder
        self._allowed = frozenset(allowed_client_cns)

    # ------------------------------------------------------------------ RPCs

    def SubmitOrder(self, request: Any, context: Any) -> Any:  # noqa: N802 - gRPC method name
        self._require_peer(context, "submit")
        att, intent = request.attestation, Intent.from_proto(request.intent)
        try:
            authorised = self._authoriser.authorise(
                bytes(att.canonical_text), bytes(att.signature), str(att.key_id), intent
            )
        except Refused as exc:
            _log.warning(
                "gateway_submit_refused",
                client_order_id=intent.client_order_id[:64],
                reason=exc.reason.value,
                detail=exc.detail[:_MAX_DETAIL],
            )
            return self._pb2.SubmitOrderResponse(reply=self._refusal(exc.reason, exc.detail))
        reply = self._forward(
            "submit",
            lambda: self._forwarder.submit(authorised.body),
            signal_id=authorised.order.signal_id,
            strategy_id=authorised.order.strategy_id,
            symbol=authorised.order.symbol,
        )
        return self._pb2.SubmitOrderResponse(reply=reply)

    def CancelOrder(self, request: Any, context: Any) -> Any:  # noqa: N802
        self._require_peer(context, "cancel")
        order_id = str(request.broker_order_id)
        reply = self._forward(
            "cancel", lambda: self._forwarder.cancel(order_id), broker_order_id=order_id[:64]
        )
        return self._pb2.CancelOrderResponse(reply=reply)

    def GetOrderByClientOrderId(self, request: Any, context: Any) -> Any:  # noqa: N802
        self._require_peer(context, "get_order")
        cid = str(request.client_order_id)
        reply = self._forward(
            "get_order",
            lambda: self._forwarder.get_order_by_client_order_id(cid),
            client_order_id=cid[:64],
        )
        return self._pb2.GetOrderByClientOrderIdResponse(reply=reply)

    def ListOpenOrders(self, request: Any, context: Any) -> Any:  # noqa: N802
        self._require_peer(context, "list_open_orders")
        reply = self._forward("list_open_orders", self._forwarder.list_open_orders)
        return self._pb2.ListOpenOrdersResponse(reply=reply)

    def GetPositions(self, request: Any, context: Any) -> Any:  # noqa: N802
        self._require_peer(context, "positions")
        return self._pb2.GetPositionsResponse(
            reply=self._forward("positions", self._forwarder.positions)
        )

    def GetAccount(self, request: Any, context: Any) -> Any:  # noqa: N802
        self._require_peer(context, "account")
        return self._pb2.GetAccountResponse(reply=self._forward("account", self._forwarder.account))

    # --------------------------------------------------------------- helpers

    def _require_peer(self, context: Any, op: str) -> None:
        names = peer_common_names(context)
        if not any(name in self._allowed for name in names):
            _log.error("gateway_peer_refused", op=op, peer_cns=list(names)[:4])
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "client identity not allowed")
            raise PermissionError("client identity not allowed")  # abort raises; defensive

    def _refusal(self, reason: Refusal, detail: str = "") -> Any:
        return self._pb2.BrokerReply(
            refusal=reason.value, detail=detail[:_MAX_DETAIL], base_url=self._forwarder.base_url
        )

    def _forward(self, op: str, call: Callable[[], HttpReply], **ids: str) -> Any:
        try:
            result = call()
        except InvalidIdentifier as exc:
            _log.warning("gateway_request_refused", op=op, reason="malformed_request", **ids)
            return self._refusal(Refusal.MALFORMED_REQUEST, f"invalid {exc}")
        except BrokerTransportError as exc:
            _log.error("gateway_broker_transport_error", op=op, error=str(exc), **ids)
            return self._pb2.BrokerReply(transport_error=True, base_url=self._forwarder.base_url)
        _log.info("gateway_forwarded", op=op, http_status=result.status, **ids)
        return self._pb2.BrokerReply(
            http_status=result.status, body=result.body, base_url=self._forwarder.base_url
        )
