"""Compile the real protos at test time and check enum + field parity with the models."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from cognitive_core.models import RegimeLabel, SignalSide, SignalStatus, TradeSignal
from cognitive_core.proto_mapping import WIRE_ONLY_FIELDS, from_proto, to_nanos, to_proto
from grpc_tools import protoc
from pydantic import ValidationError

PROTO_DIR = Path(__file__).resolve().parents[3] / "shared" / "proto"

# Numeric values transcribed from trade_signal.proto / market_snapshot.proto.
EXPECTED_SIDE = {"SIDE_UNKNOWN": 0, "BUY": 1, "SELL": 2, "SELL_SHORT": 3}
EXPECTED_STATUS = {
    "SIGNAL_PENDING": 0,
    "SIGNAL_APPROVED": 1,
    "SIGNAL_REJECTED_HARD_BLOCK": 2,
    "SIGNAL_SOFT_BLOCK_PENDING": 3,
    "SIGNAL_ABSTAIN": 4,
    "SIGNAL_EXPIRED": 5,
}
EXPECTED_REGIME = {
    "REGIME_UNKNOWN": 0,
    "TRENDING_BULL": 1,
    "TRENDING_BEAR": 2,
    "HIGH_VOL_CHOP": 3,
    "LOW_VOL_CHOP": 4,
    "CRISIS": 5,
}


@pytest.fixture(scope="module")
def pb2(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ModuleType]:
    out = tmp_path_factory.mktemp("proto_out")
    rc = protoc.main(
        [
            "protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={out}",
            str(PROTO_DIR / "market_snapshot.proto"),
            str(PROTO_DIR / "trade_signal.proto"),
        ]
    )
    assert rc == 0, "protoc failed to compile the shared protos"
    sys.path.insert(0, str(out))
    try:
        importlib.import_module("market_snapshot_pb2")
        yield importlib.import_module("trade_signal_pb2")
    finally:
        sys.path.remove(str(out))
        for name in ("trade_signal_pb2", "market_snapshot_pb2"):
            sys.modules.pop(name, None)


def _enum_values(pb2: ModuleType, message: str, field: str) -> dict[str, int]:
    descriptor = getattr(pb2, message).DESCRIPTOR.fields_by_name[field].enum_type
    return {v.name: v.number for v in descriptor.values}


@pytest.mark.parametrize(
    ("field", "enum_cls", "expected"),
    [
        ("side", SignalSide, EXPECTED_SIDE),
        ("status", SignalStatus, EXPECTED_STATUS),
        ("regime", RegimeLabel, EXPECTED_REGIME),
    ],
)
def test_enum_names_and_numbers_match_proto(
    pb2: ModuleType, field: str, enum_cls: type, expected: dict[str, int]
) -> None:
    compiled = _enum_values(pb2, "TradeSignal", field)
    assert compiled == expected
    assert {m.value for m in enum_cls} == set(compiled)  # type: ignore[attr-defined]


def test_regime_enum_matches_market_snapshot_message(pb2: ModuleType) -> None:
    market = importlib.import_module("market_snapshot_pb2")
    snapshot = market.MarketSnapshot.DESCRIPTOR.fields_by_name["regime"].enum_type
    assert {v.name: v.number for v in snapshot.values} == EXPECTED_REGIME


def test_every_proto_field_is_modelled(pb2: ModuleType) -> None:
    proto_fields = set(pb2.TradeSignal.DESCRIPTOR.fields_by_name)
    assert proto_fields == set(TradeSignal.model_fields) | WIRE_ONLY_FIELDS
    assert not WIRE_ONLY_FIELDS & set(TradeSignal.model_fields)


def _pending() -> TradeSignal:
    return TradeSignal(
        signal_id="sig-1", symbol="AAPL", created_at_ns=1_700_000_000_000_000_000,
        side=SignalSide.SELL_SHORT, quantity=25.0, omega=0.81, expected_value=12.5,
        p_success=0.62, p_failure=0.38, reward_estimate=100.0, risk_estimate=40.0,
        estimated_spread_cost=0.5, estimated_market_impact=0.25, estimated_venue_fees=0.1,
        estimated_total_cost=0.85, regime=RegimeLabel.HIGH_VOL_CHOP, regime_confidence=0.7,
        debate_summary="summary", price_limit=189.5,
        valid_until_ns=1_700_000_002_000_000_000, status=SignalStatus.SIGNAL_PENDING,
    )


def test_round_trip_preserves_every_field(pb2: ModuleType) -> None:
    original = _pending()
    message = to_proto(original, pb2)
    assert message.side == 3 and message.regime == 3 and message.status == 0
    assert message.created_at_ns == original.created_at_ns
    assert message.valid_until_ns == original.valid_until_ns
    assert message.estimated_total_cost == pytest.approx(0.85)
    restored = from_proto(pb2.TradeSignal.FromString(message.SerializeToString()), pb2)
    assert restored == original


def test_round_trip_abstain_with_regime_unknown(pb2: ModuleType) -> None:
    original = _pending().model_copy(
        update={
            "side": SignalSide.SIDE_UNKNOWN, "quantity": 0.0,
            "status": SignalStatus.SIGNAL_ABSTAIN, "regime": RegimeLabel.REGIME_UNKNOWN,
        }
    )
    assert from_proto(to_proto(original, pb2), pb2) == original


def test_from_proto_rejects_actionable_signal_with_zero_quantity(pb2: ModuleType) -> None:
    message = to_proto(_pending(), pb2)
    message.quantity = 0.0
    message.quantity_nanos = 0
    with pytest.raises(ValidationError):
        from_proto(message, pb2)


def test_from_proto_rejects_unmapped_enum_number(pb2: ModuleType) -> None:
    message = to_proto(_pending(), pb2)
    message.side = 99
    with pytest.raises(ValueError, match="not defined"):
        from_proto(message, pb2)


def test_to_proto_rejects_unknown_enum_name(pb2: ModuleType) -> None:
    class _Fake:
        """A signal-like object whose enum value has no proto counterpart."""

        def __getattr__(self, name: str) -> Any:
            if name == "side":
                return "SIDEWAYS"
            return getattr(_pending(), name)

    with pytest.raises(ValueError, match="not defined"):
        to_proto(_Fake(), pb2)  # type: ignore[arg-type]


def test_to_proto_sets_fixed_point_nanos_and_strategy_id(pb2: ModuleType) -> None:
    message = to_proto(_pending(), pb2, strategy_id="AFE-STRATEGY-001")
    assert message.quantity_nanos == 25_000_000_000
    assert message.price_limit_nanos == 189_500_000_000
    assert message.estimated_total_cost_nanos == 850_000_000
    assert message.estimated_spread_cost_nanos == 500_000_000
    assert message.estimated_market_impact_nanos == 250_000_000
    assert message.estimated_venue_fees_nanos == 100_000_000
    assert message.strategy_id == "AFE-STRATEGY-001"


def test_round_trip_survives_nanos_fields(pb2: ModuleType) -> None:
    original = _pending()
    wire = pb2.TradeSignal.FromString(to_proto(original, pb2, strategy_id="S").SerializeToString())
    assert from_proto(wire, pb2) == original


def test_from_proto_rejects_double_and_nanos_disagreement(pb2: ModuleType) -> None:
    message = to_proto(_pending(), pb2)
    message.quantity_nanos = 1_000_000_000  # 1 share vs quantity=25.0
    with pytest.raises(ValueError, match="quantity_nanos disagrees"):
        from_proto(message, pb2)


@pytest.mark.parametrize(
    ("value", "nanos"),
    [(0.0, 0), (0.1, 100_000_000), (1.5e-9, 2), (2.5e-9, 2), (189.5, 189_500_000_000)],
)
def test_to_nanos_uses_exact_decimal_rounding(value: float, nanos: int) -> None:
    assert to_nanos(value) == nanos


def test_to_nanos_rejects_int64_overflow_and_negatives() -> None:
    with pytest.raises(ValueError, match="int64"):
        to_nanos(1e10)
    with pytest.raises(ValueError, match="int64"):
        to_nanos(-1.0)
