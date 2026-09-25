"""Listing open orders (the input to the kill-switch cancel sweep, ALI-162).

Alpaca's list shape is ASSUMED from the public v2 REST docs: GET /v2/orders?status=open
returns a JSON array of order objects.
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from execution_motor.broker import BrokerOrderRequest
from execution_motor.errors import BrokerReadError
from execution_motor.mock_broker import MockBroker
from execution_motor.models import ExecutionStatus, OrderType, Side
from tests.test_alpaca_http import Script, jresp, make_broker, order_json


def test_list_open_orders_queries_open_status_and_parses_every_order() -> None:
    script = Script(
        jresp(
            200,
            [
                order_json(),
                order_json(id="a1b2c3d4-0000-4000-8000-000000000002", client_order_id="c2"),
            ],
        )
    )
    orders = make_broker(script).list_open_orders()

    assert [o.broker_order_id for o in orders] == [
        "904837e3-3b76-47ec-b432-046db621571b",
        "a1b2c3d4-0000-4000-8000-000000000002",
    ]
    request = script.requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v2/orders"
    assert request.url.params["status"] == "open"
    assert request.url.params["limit"] == "500"


def test_list_open_orders_empty() -> None:
    assert make_broker(Script(jresp(200, []))).list_open_orders() == ()


def test_list_open_orders_fails_loud_on_a_malformed_item() -> None:
    # A silently skipped item would be an order the kill switch never cancels.
    script = Script(jresp(200, [order_json(), "not-an-order"]))
    with pytest.raises(BrokerReadError):
        make_broker(script).list_open_orders()


def test_list_open_orders_fails_loud_when_body_is_not_a_list() -> None:
    with pytest.raises(BrokerReadError):
        make_broker(Script(jresp(200, {"orders": []}))).list_open_orders()


def test_list_open_orders_retries_like_every_safe_read() -> None:
    script = Script(httpx.ConnectError("down"), jresp(200, [order_json()]))
    sleeps: list[float] = []
    assert len(make_broker(script, sleeps=sleeps).list_open_orders()) == 1
    assert len(sleeps) == 1


def test_mock_broker_has_no_open_orders_because_it_fills_immediately() -> None:
    broker = MockBroker()
    broker.submit_order(
        BrokerOrderRequest(
            client_order_id="c1",
            symbol="AAPL",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("10"),
            limit_price=Decimal("190"),
        )
    )
    assert broker.list_open_orders() == ()
    found = broker.get_order_by_client_id("c1")
    assert found is not None and found.status is ExecutionStatus.FILLED
