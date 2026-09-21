"""TradeSignal <-> `shared/proto/trade_signal.proto` mapping.

Generated stubs are injected (`pb2` = the compiled `trade_signal_pb2` module) so this
module has no import-time dependency on a generated-code location. Enums are
mapped by NAME through the message descriptors, so a renumbered or renamed proto value
fails loudly (ValueError) rather than silently mapping to the wrong member.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from .models import RegimeLabel, SignalSide, SignalStatus, TradeSignal

# Scalar TradeSignal fields that map 1:1 by name between pydantic and proto.
_SCALAR_FIELDS = (
    "signal_id",
    "symbol",
    "created_at_ns",
    "quantity",
    "omega",
    "expected_value",
    "p_success",
    "p_failure",
    "reward_estimate",
    "risk_estimate",
    "estimated_spread_cost",
    "estimated_market_impact",
    "estimated_venue_fees",
    "estimated_total_cost",
    "regime_confidence",
    "debate_summary",
    "price_limit",
    "valid_until_ns",
)


_ENUM_FIELDS = ("side", "status", "regime")

# ALI-22: fixed-point (1e-9) counterparts of the double money fields. The proto says
# producers should set these; Aegis ignores the deprecated doubles once migrated.
NANOS = 1_000_000_000
INT64_MAX = 2**63 - 1
_NANOS_FIELDS = {
    "quantity_nanos": "quantity",
    "price_limit_nanos": "price_limit",
    "estimated_total_cost_nanos": "estimated_total_cost",
    "estimated_spread_cost_nanos": "estimated_spread_cost",
    "estimated_market_impact_nanos": "estimated_market_impact",
    "estimated_venue_fees_nanos": "estimated_venue_fees",
}
# Proto fields with no pydantic counterpart: derived at the wire boundary only.
WIRE_ONLY_FIELDS = frozenset({*_NANOS_FIELDS, "strategy_id"})


def to_nanos(value: float) -> int:
    """Exact decimal conversion of a finite non-negative float to 1e-9 fixed point."""
    nanos = int((Decimal(repr(float(value))) * NANOS).to_integral_value(ROUND_HALF_EVEN))
    if not 0 <= nanos <= INT64_MAX:
        raise ValueError(f"{value!r} does not fit a non-negative int64 in 1e-9 units")
    return nanos


def _enum_number(message: Any, field: str, name: str) -> int:
    values = message.DESCRIPTOR.fields_by_name[field].enum_type.values_by_name
    if name not in values:
        raise ValueError(f"{field}: {name!r} is not defined in the proto enum")
    return int(values[name].number)


def _enum_name(message: Any, field: str, number: int) -> str:
    values = message.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number
    if number not in values:
        raise ValueError(f"{field}: {number} is not defined in the proto enum")
    return str(values[number].name)


def to_proto(signal: TradeSignal, pb2: Any, *, strategy_id: str = "") -> Any:
    """Build a `pb2.TradeSignal` message.

    Fills the deprecated double fields AND the ALI-22 `*_nanos` fields plus `strategy_id`
    (used by Aegis regime gate C18). Raises ValueError on an unknown enum name or a value
    that does not fit int64 nanos.
    """
    message = pb2.TradeSignal()
    for name in _SCALAR_FIELDS:
        setattr(message, name, getattr(signal, name))
    for nanos_field, double_field in _NANOS_FIELDS.items():
        setattr(message, nanos_field, to_nanos(getattr(signal, double_field)))
    message.strategy_id = strategy_id
    for field in _ENUM_FIELDS:
        setattr(message, field, _enum_number(message, field, str(getattr(signal, field))))
    return message


def from_proto(message: Any, pb2: Any) -> TradeSignal:
    """Validate a `pb2.TradeSignal` message into a `TradeSignal`.

    Raises ValueError / pydantic.ValidationError for an unmapped enum value or a
    signal violating the model invariants (e.g. actionable with quantity 0).
    """
    for nanos_field, double_field in _NANOS_FIELDS.items():
        wire_nanos = getattr(message, nanos_field)
        expected = to_nanos(getattr(message, double_field))
        # Fail closed if the two representations disagree by more than rounding.
        if abs(wire_nanos - expected) > 1:
            raise ValueError(f"{nanos_field} disagrees with {double_field}")
    values: dict[str, Any] = {name: getattr(message, name) for name in _SCALAR_FIELDS}
    values["side"] = SignalSide(_enum_name(message, "side", message.side))
    values["status"] = SignalStatus(_enum_name(message, "status", message.status))
    values["regime"] = RegimeLabel(_enum_name(message, "regime", message.regime))
    return TradeSignal(**values)
