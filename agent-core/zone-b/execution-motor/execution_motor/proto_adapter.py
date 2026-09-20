"""Convert generated ``afe.shared`` messages (OrderRequest + Attestation) into the domain model.

This is the ONLY module that touches protobuf messages. It is duck-typed (no import of the
generated stubs) so the package does not depend on where the infra build puts them.

Money contract (integration/wave1): quantity/prices are int64 "nanos" (1e-9). They are turned
into Decimal exactly at this edge. The deprecated double fields (7, 8, 9) are IGNORED: they
are not part of the signed text, so they are unauthenticated.

Signing contract (ASSUMED from docs/specs/phase_3_aegis_execution.md section 7,
"afe-attest-v1", marked PROPOSED there; the Aegis crate has not defined it yet):
  text = "afe-attest-v1\\n" then one ``key=value\\n`` line each, in this order:
         signal_id, symbol, side (BUY|SELL), order_type (MARKET|LIMIT|STOP|STOP_LIMIT),
         qty_nanos, limit_price_nanos, stop_price_nanos, decided_at_ns, expires_at_ns,
         aegis_state_seq, limits_config_sha256, key_id
  Attestation.payload_sha256 = SHA-256(text); signature is over that payload under key_id
  (ECDSA P-256/SHA-256 proposed). Not signed, hence untrusted hints only: order_id,
  created_at_ns, preferred_venue, max_venue_toxicity, algo and the other algo fields.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from .errors import OrderValidationError
from .models import Attestation, AttestedOrder, ExecAlgo, Order, OrderType, Side

CANONICAL_VERSION: Final = "afe-attest-v1"
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


def canonical_attestation_text(order: Any, attestation: Any) -> bytes:
    """Rebuild the text Aegis is expected to have signed, from the received fields."""
    side_name = _lookup(_SIDES, order.side, "side")[0]
    type_name = _lookup(_TYPES, order.order_type, "order_type")[0]
    lines = (
        CANONICAL_VERSION,
        f"signal_id={order.signal_id}",
        f"symbol={order.symbol}",
        f"side={side_name}",
        f"order_type={type_name}",
        f"qty_nanos={int(order.quantity_nanos)}",
        f"limit_price_nanos={int(order.limit_price_nanos)}",
        f"stop_price_nanos={int(order.stop_price_nanos)}",
        f"decided_at_ns={int(attestation.decided_at_ns)}",
        f"expires_at_ns={int(attestation.expires_at_ns)}",
        f"aegis_state_seq={int(attestation.aegis_state_seq)}",
        f"limits_config_sha256={attestation.limits_config_sha256}",
        f"key_id={attestation.key_id}",
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


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


def attested_order_from_proto(order: Any, attestation: Any) -> AttestedOrder:
    """Validate and convert. Raises OrderValidationError on anything ambiguous."""
    _check_consistency(order, attestation)
    domain_order = _build_order(order)
    return AttestedOrder(
        order=domain_order,
        attestation=Attestation(
            signature=bytes(attestation.signature),
            key_id=attestation.key_id,
            signed_payload=canonical_attestation_text(order, attestation),
            payload_sha256=bytes(attestation.payload_sha256),
            decided_at_ns=int(attestation.decided_at_ns),
            expires_at_ns=int(attestation.expires_at_ns),
        ),
    )
