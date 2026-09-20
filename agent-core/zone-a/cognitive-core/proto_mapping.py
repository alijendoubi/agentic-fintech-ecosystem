"""TradeSignal <-> `shared/proto/trade_signal.proto` mapping.

Generated stubs are injected (`pb2` = the compiled `trade_signal_pb2` module) so this
module has no import-time dependency on a generated-code location. Enums are
mapped by NAME through the message descriptors, so a renumbered or renamed proto value
fails loudly (ValueError) rather than silently mapping to the wrong member.
"""

from __future__ import annotations

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


def to_proto(signal: TradeSignal, pb2: Any) -> Any:
    """Build a `pb2.TradeSignal` message. Raises ValueError on an unknown enum name."""
    message = pb2.TradeSignal()
    for name in _SCALAR_FIELDS:
        setattr(message, name, getattr(signal, name))
    for field in _ENUM_FIELDS:
        setattr(message, field, _enum_number(message, field, str(getattr(signal, field))))
    return message


def from_proto(message: Any, pb2: Any) -> TradeSignal:
    """Validate a `pb2.TradeSignal` message into a `TradeSignal`.

    Raises ValueError / pydantic.ValidationError for an unmapped enum value or a
    signal violating the model invariants (e.g. actionable with quantity 0).
    """
    values: dict[str, Any] = {name: getattr(message, name) for name in _SCALAR_FIELDS}
    values["side"] = SignalSide(_enum_name(message, "side", message.side))
    values["status"] = SignalStatus(_enum_name(message, "status", message.status))
    values["regime"] = RegimeLabel(_enum_name(message, "regime", message.regime))
    return TradeSignal(**values)
