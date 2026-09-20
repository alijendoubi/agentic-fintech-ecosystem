"""Convert a generated ``afe.shared.OrderRequest`` into the domain model.

This is the ONLY module that touches protobuf messages. It is duck-typed (no import of the
generated stubs) so the package does not depend on where the infra build puts them.

Signing contract (v1, ASSUMED - Aegis must implement the same bytes; see report):
    payload = SIGNING_DOMAIN + OrderRequest.SerializeToString(deterministic=True)
              with ``hsm_signature`` and ``status`` cleared.
``hsm_key_id`` stays inside the signed bytes, binding the key id to the signature.
The proto comment says "PKCS#11 ECDSA over canonical order bytes" but does not define them.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from .errors import OrderValidationError
from .models import Attestation, AttestedOrder, ExecAlgo, Order, OrderType, Side

SIGNING_DOMAIN: Final = b"AFE-ORDER-v1\x00"

# Numeric values of afe.shared enums (order_request.proto). Unknown/0 values are refused.
_SIDES: Final = {1: Side.BUY, 2: Side.SELL}
_TYPES: Final = {
    1: OrderType.MARKET,
    2: OrderType.LIMIT,
    3: OrderType.STOP,
    4: OrderType.STOP_LIMIT,
}
_ALGOS: Final = {1: ExecAlgo.DIRECT, 2: ExecAlgo.ICEBERG, 3: ExecAlgo.POV}
_STATUS_PENDING: Final = 0


def canonical_signing_payload(msg: Any) -> bytes:
    """Bytes Aegis is expected to have signed for this message."""
    clone = type(msg)()
    clone.CopyFrom(msg)
    clone.ClearField("hsm_signature")
    clone.ClearField("status")
    return SIGNING_DOMAIN + bytes(clone.SerializeToString(deterministic=True))


def _decimal(value: float, name: str) -> Decimal:
    if not math.isfinite(value):
        raise OrderValidationError(f"{name} is not finite")
    return Decimal(str(value))


def _enum(mapping: dict[int, Any], value: int, name: str) -> Any:
    try:
        return mapping[int(value)]
    except KeyError as exc:
        raise OrderValidationError(f"{name} has unsupported value {value}") from exc


def attested_order_from_proto(msg: Any) -> AttestedOrder:
    """Validate and convert. Raises OrderValidationError on anything ambiguous."""
    if int(msg.status) != _STATUS_PENDING:
        raise OrderValidationError("OrderRequest.status must be ORDER_PENDING on ingress")
    try:
        order = Order(
            order_id=msg.order_id,
            signal_id=msg.signal_id,
            symbol=msg.symbol,
            created_at_ns=int(msg.created_at_ns),
            side=_enum(_SIDES, msg.side, "side"),
            order_type=_enum(_TYPES, msg.order_type, "order_type"),
            quantity=_decimal(msg.quantity, "quantity"),
            limit_price=_decimal(msg.limit_price, "limit_price"),
            stop_price=_decimal(msg.stop_price, "stop_price"),
            preferred_venue=msg.preferred_venue,
            max_venue_toxicity=_decimal(msg.max_venue_toxicity, "max_venue_toxicity"),
            algo=_enum(_ALGOS, msg.algo, "algo"),
            pov_target_rate=_decimal(msg.pov_target_rate, "pov_target_rate"),
            iceberg_display_size=_decimal(msg.iceberg_display_size, "iceberg_display_size"),
        )
    except ValidationError as exc:
        raise OrderValidationError(f"invalid OrderRequest: {exc.error_count()} field error(s)") from exc
    attestation = Attestation(
        signature=bytes(msg.hsm_signature),
        key_id=msg.hsm_key_id,
        signed_payload=canonical_signing_payload(msg),
    )
    return AttestedOrder(order=order, attestation=attestation)
