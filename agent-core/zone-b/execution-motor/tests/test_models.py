from __future__ import annotations

from decimal import Decimal

import pytest
from execution_motor.models import (
    ExecutionReport,
    ExecutionStatus,
    Fill,
    OrderType,
    RejectReason,
)
from pydantic import ValidationError

from .helpers import make_order


def test_valid_limit_order_builds() -> None:
    order = make_order()
    assert order.quantity == Decimal("10")
    assert order.client_order_id == order.order_id


def test_order_is_immutable() -> None:
    order = make_order()
    with pytest.raises(ValidationError):
        order.quantity = Decimal("11")


@pytest.mark.parametrize("qty", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_bad_quantity_rejected(qty: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_order(quantity=qty)


def test_negative_price_rejected() -> None:
    with pytest.raises(ValidationError):
        make_order(limit_price=Decimal("-1"))


def test_limit_order_requires_limit_price() -> None:
    with pytest.raises(ValidationError):
        make_order(order_type=OrderType.LIMIT, limit_price=Decimal("0"))


def test_market_order_must_not_carry_prices() -> None:
    with pytest.raises(ValidationError):
        make_order(order_type=OrderType.MARKET, limit_price=Decimal("5"))
    order = make_order(order_type=OrderType.MARKET, limit_price=Decimal("0"))
    assert order.order_type is OrderType.MARKET


def test_stop_requires_stop_price_and_stop_limit_requires_both() -> None:
    with pytest.raises(ValidationError):
        make_order(order_type=OrderType.STOP, limit_price=Decimal("0"), stop_price=Decimal("0"))
    with pytest.raises(ValidationError):
        make_order(
            order_type=OrderType.STOP_LIMIT, limit_price=Decimal("0"), stop_price=Decimal("9")
        )
    ok = make_order(
        order_type=OrderType.STOP_LIMIT, limit_price=Decimal("8"), stop_price=Decimal("9")
    )
    assert ok.stop_price == Decimal("9")


@pytest.mark.parametrize("symbol", ["", "aapl", "AAPL; DROP", "A" * 20, "../x"])
def test_bad_symbol_rejected(symbol: str) -> None:
    with pytest.raises(ValidationError):
        make_order(symbol=symbol)


@pytest.mark.parametrize("oid", ["", "has space", "x" * 65, "a/b"])
def test_bad_order_id_rejected(oid: str) -> None:
    with pytest.raises(ValidationError):
        make_order(order_id=oid)


def test_toxicity_bound_must_be_unit_interval() -> None:
    with pytest.raises(ValidationError):
        make_order(max_venue_toxicity=Decimal("1.5"))


def test_created_at_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        make_order(created_at_ns=0)


def _report(**overrides: object) -> ExecutionReport:
    fields: dict[str, object] = {
        "order_id": "o1",
        "client_order_id": "o1",
        "signal_id": "s1",
        "symbol": "AAPL",
        "status": ExecutionStatus.REJECTED,
        "requested_quantity": Decimal("10"),
        "reject_reason": RejectReason.HALTED,
        "received_at_ns": 1,
        "updated_at_ns": 2,
    }
    fields.update(overrides)
    return ExecutionReport(**fields)  # type: ignore[arg-type]


def test_rejected_report_requires_reason() -> None:
    with pytest.raises(ValidationError):
        _report(reject_reason=None)


def test_non_rejected_report_must_not_carry_reject_reason() -> None:
    with pytest.raises(ValidationError):
        _report(status=ExecutionStatus.ACCEPTED)


def test_filled_quantity_and_avg_price_derive_from_fills() -> None:
    fills = (
        Fill(quantity=Decimal("4"), price=Decimal("10.00"), timestamp_ns=5, venue="V"),
        Fill(quantity=Decimal("6"), price=Decimal("11.00"), timestamp_ns=6, venue="V"),
    )
    report = _report(status=ExecutionStatus.FILLED, reject_reason=None, fills=fills, venue="V")
    assert report.filled_quantity == Decimal("10")
    assert report.avg_fill_price == Decimal("10.60")


def test_unfilled_report_has_no_avg_price() -> None:
    assert _report().avg_fill_price is None
    assert _report().filled_quantity == Decimal("0")


def test_slippage_bps_uses_arrival_price_and_side() -> None:
    fills = (Fill(quantity=Decimal("10"), price=Decimal("100.10"), timestamp_ns=5, venue="V"),)
    report = _report(
        status=ExecutionStatus.FILLED,
        reject_reason=None,
        fills=fills,
        side="buy",
        arrival_price=Decimal("100.00"),
    )
    assert report.slippage_bps == Decimal("10")
    sell = report.model_copy(update={"side": "sell"})
    assert sell.slippage_bps == Decimal("-10")


def test_fill_rejects_non_positive_values() -> None:
    with pytest.raises(ValidationError):
        Fill(quantity=Decimal("0"), price=Decimal("1"), timestamp_ns=1, venue="V")
    with pytest.raises(ValidationError):
        Fill(quantity=Decimal("1"), price=Decimal("0"), timestamp_ns=1, venue="V")
