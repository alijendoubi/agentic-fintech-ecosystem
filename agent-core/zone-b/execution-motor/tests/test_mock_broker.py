from __future__ import annotations

from decimal import Decimal

from execution_motor.broker import BrokerOrderRequest
from execution_motor.mock_broker import MockBroker
from execution_motor.models import ExecutionStatus, OrderType, Side


def test_submit_order_fills_immediately_at_limit_price() -> None:
    broker = MockBroker(clock_ns=lambda: 42)
    request = BrokerOrderRequest(
        client_order_id="c1",
        symbol="AAPL",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("10"),
        limit_price=Decimal("190.50"),
    )
    order = broker.submit_order(request)
    assert order.status is ExecutionStatus.FILLED
    assert order.filled_quantity == Decimal("10")
    assert order.filled_avg_price == Decimal("190.50")
    assert order.submitted_at_ns == 42


def test_submit_order_uses_placeholder_price_for_market_order() -> None:
    broker = MockBroker()
    request = BrokerOrderRequest(
        client_order_id="c2",
        symbol="AAPL",
        side=Side.SELL,
        order_type=OrderType.MARKET,
        quantity=Decimal("5"),
    )
    order = broker.submit_order(request)
    assert order.filled_avg_price is not None
    assert order.filled_avg_price > 0


def test_get_order_by_client_id_round_trips() -> None:
    broker = MockBroker()
    request = BrokerOrderRequest(
        client_order_id="c3",
        symbol="AAPL",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
    )
    broker.submit_order(request)
    assert broker.get_order_by_client_id("c3") is not None
    assert broker.get_order_by_client_id("unknown") is None


def test_venue_and_reads_are_safe_defaults() -> None:
    broker = MockBroker(venue="mock-dev")
    assert broker.venue == "mock-dev"
    assert broker.get_positions() == ()
    account = broker.get_account()
    assert account.trading_blocked is False
    broker.cancel_order("anything")  # never raises
