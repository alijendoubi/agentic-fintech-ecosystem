"""Convert generated ``afe.shared`` messages (OrderRequest + Attestation) into the domain model.

This is the ONLY module that touches protobuf messages. It is duck-typed (no import of the
generated stubs) so the package does not depend on where the infra build puts them.

Money contract (integration/wave1): quantity/prices are int64 "nanos" (1e-9). They are turned
into Decimal exactly at this edge. The deprecated double fields (7, 8, 9) are IGNORED: they
are not part of the signed text, so they are unauthenticated.

Signing contract: Aegis is the source of truth (``canonical.py`` documents the text and is
verified against fixtures produced by the real Aegis crate). Not signed, hence untrusted hints
only: created_at_ns, preferred_venue, max_venue_toxicity, algo and the other algo fields.
``order_id`` is not in the text, but Aegis sets ``order_id == signal_id``; this adapter refuses
any order where they differ.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from .canonical import CANONICAL_VERSION, build_canonical_text, candidate_side_names
from .errors import OrderValidationError
from .models import Attestation, AttestedOrder, ExecAlgo, Order, OrderType, Side

_NANOS: Final = 9

# Numeric values of afe.shared enums (order_request.proto). Unknown/0 values are refused.
_SIDES: Final = {1: ("BUY", Side.BUY), 2: ("SELL", Side.SELL)}
_TYPES: Final = {
    1: ("MARKET", OrderType.MARKET),
    2: ("LIMIT", OrderType.LIMIT),
    3: ("STOP", OrderType.STOP),
    4: ("STOP_LIMIT", OrderType.STOP_LIMIT),
}
_ALGOS: Final = {1: ExecAlgo.DIRECT, 2: ExecAlgo.ICEBERG, 3: ExecAlgo.POV}
_STATUS_PENDING: Final = 0


def from_nanos(value: int, name: str) -> Decimal:
    """Exact int64 nanos -> Decimal. Negative values are refused."""
    if value < 0:
        raise OrderValidationError(f"{name} must not be negative")
    return Decimal(int(value)).scaleb(-_NANOS).normalize()


def _double_hint(value: float, name: str) -> Decimal:
    """Unsigned routing-hint doubles (toxicity bound, algo params): finite, via str()."""
    text = repr(float(value))
    if text in ("nan", "inf", "-inf"):
        raise OrderValidationError(f"{name} is not finite")
    return Decimal(text)


def _lookup(mapping: dict[int, Any], value: int, name: str) -> Any:
    try:
        return mapping[int(value)]
    except KeyError as exc:
        raise OrderValidationError(f"{name} has unsupported value {value}") from exc


def canonical_attestation_text(
    order: Any, attestation: Any, *, side_name: str | None = None
) -> bytes:
    """Rebuild the text Aegis signed from the received fields.

    ``side_name`` selects BUY/SELL/SELL_SHORT; the default is the first candidate for the
    order's side (BUY for ORDER_BUY, SELL for ORDER_SELL).
    """
    type_name = _lookup(_TYPES, order.order_type, "order_type")[0]
    if side_name is None:
        side_name = candidate_side_names(int(order.side), allow_short=False)[0]
    return build_canonical_text(
        signal_id=order.signal_id,
        symbol=order.symbol,
        side=side_name,
        order_type=type_name,
        qty_nanos=order.quantity_nanos,
        limit_price_nanos=order.limit_price_nanos,
        stop_price_nanos=order.stop_price_nanos,
        decided_at_ns=attestation.decided_at_ns,
        expires_at_ns=attestation.expires_at_ns,
        aegis_state_seq=attestation.aegis_state_seq,
        limits_config_sha256=attestation.limits_config_sha256,
        key_id=attestation.key_id,
    )


def _select_signed_text(order: Any, attestation: Any, *, allow_short: bool) -> tuple[bytes, str]:
    """The candidate text whose SHA-256 equals ``payload_sha256`` (SELL first, then SELL_SHORT).

    No match -> the first candidate is returned so ``evaluate_attestation`` denies on the
    digest mismatch (fail closed). The signature is verified later, over what this returns.
    """
    claimed = bytes(attestation.payload_sha256)
    first: tuple[bytes, str] | None = None
    for name in candidate_side_names(int(order.side), allow_short=allow_short):
        text = canonical_attestation_text(order, attestation, side_name=name)
        if hmac.compare_digest(hashlib.sha256(text).digest(), claimed):
            return text, name
        first = first or (text, name)
    if first is None:  # candidate_side_names never returns an empty tuple; defensive
        raise OrderValidationError("no candidate side")
    return first


def _check_consistency(order: Any, attestation: Any) -> None:
    if int(order.status) != _STATUS_PENDING:
        raise OrderValidationError("OrderRequest.status must be ORDER_PENDING on ingress")
    if attestation.canonical_version != CANONICAL_VERSION:
        raise OrderValidationError("unsupported attestation canonical_version")
    # AegisDecision.order mirrors the attestation; any disagreement means the pair is not
    # what Aegis produced.
    if (
        bytes(order.hsm_signature) != bytes(attestation.signature)
        or order.hsm_key_id != attestation.key_id
        or int(order.attestation_expires_at_ns) != int(attestation.expires_at_ns)
    ):
        raise OrderValidationError("OrderRequest does not mirror its Attestation")


def _build_order(msg: Any) -> Order:
    try:
        return Order(
            order_id=msg.order_id,
            signal_id=msg.signal_id,
            symbol=msg.symbol,
            created_at_ns=int(msg.created_at_ns),
            side=_lookup(_SIDES, msg.side, "side")[1],
            order_type=_lookup(_TYPES, msg.order_type, "order_type")[1],
            quantity=from_nanos(int(msg.quantity_nanos), "quantity_nanos"),
            limit_price=from_nanos(int(msg.limit_price_nanos), "limit_price_nanos"),
            stop_price=from_nanos(int(msg.stop_price_nanos), "stop_price_nanos"),
            preferred_venue=msg.preferred_venue,
            max_venue_toxicity=_double_hint(msg.max_venue_toxicity, "max_venue_toxicity"),
            algo=_lookup(_ALGOS, msg.algo, "algo"),
            pov_target_rate=_double_hint(msg.pov_target_rate, "pov_target_rate"),
            iceberg_display_size=_double_hint(msg.iceberg_display_size, "iceberg_display_size"),
        )
    except ValidationError as exc:
        raise OrderValidationError(
            f"invalid OrderRequest: {exc.error_count()} field error(s)"
        ) from exc


def attested_order_from_proto(
    order: Any, attestation: Any, *, allow_short: bool = False
) -> AttestedOrder:
    """Validate and convert. Raises OrderValidationError on anything ambiguous.

    Side mapping: ORDER_BUY <-> BUY; ORDER_SELL <-> SELL, or SELL_SHORT when ``allow_short``
    (the signed digest decides which one Aegis signed). With shorts disabled a SELL_SHORT
    attestation never matches and the order is denied as ATTESTATION_INVALID (fail closed).
    """
    _check_consistency(order, attestation)
    domain_order = _build_order(order)
    text, side_name = _select_signed_text(order, attestation, allow_short=allow_short)
    return AttestedOrder(
        order=domain_order,
        attestation=Attestation(
            signature=bytes(attestation.signature),
            key_id=attestation.key_id,
            signed_payload=text,
            payload_sha256=bytes(attestation.payload_sha256),
            decided_at_ns=int(attestation.decided_at_ns),
            expires_at_ns=int(attestation.expires_at_ns),
            attested_side=side_name,
        ),
    )
