"""AlpacaPaperBroker behaviour over httpx.MockTransport. No network is ever touched.

Alpaca response shapes are ASSUMED from the public v2 REST docs (see execution_motor.alpaca).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest

from execution_motor.alpaca import AlpacaCredentials, AlpacaPaperBroker
from execution_motor.broker import BrokerOrderRequest
from execution_motor.errors import (
    BrokerError,
    BrokerReadError,
    BrokerRejectedError,
    SubmitOutcomeUnknown,
)
from execution_motor.models import ExecutionStatus, OrderType, Side

CREDS = AlpacaCredentials(api_key="PKTESTKEY123", secret_key="SECRETVALUE456")
CID = "0b9c7f3e-6d0e-4a3a-9c53-0d9d5b8e1a11"


def order_json(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "904837e3-3b76-47ec-b432-046db621571b",
        "client_order_id": CID,
        "created_at": "2026-09-20T10:00:00.123456789Z",
        "submitted_at": "2026-09-20T10:00:00.223456789Z",
        "filled_at": None,
        "symbol": "AAPL",
        "qty": "10",
        "filled_qty": "0",
        "filled_avg_price": None,
        "type": "limit",
        "side": "buy",
        "status": "new",
        "limit_price": "190.50",
    }
    body.update(overrides)
    return body


def jresp(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, text=json.dumps(body), headers={"content-type": "application/json"})


class Script:
    """Feeds scripted responses/exceptions in order and records every request."""

    def __init__(self, *steps: httpx.Response | Exception) -> None:
        self._steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._steps:
            raise AssertionError(f"unexpected extra request: {request.method} {request.url}")
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    def methods(self) -> list[str]:
        return [r.method for r in self.requests]


def make_broker(
    handler: Callable[[httpx.Request], httpx.Response],
    sleeps: list[float] | None = None,
    retries: int = 3,
) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        CREDS,
        transport=httpx.MockTransport(handler),
        sleep=(sleeps if sleeps is not None else []).append,
        rng=lambda: 0.5,
        read_retries=retries,
    )


def request(**overrides: Any) -> BrokerOrderRequest:
    fields: dict[str, Any] = {
        "client_order_id": CID,
        "symbol": "AAPL",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": Decimal("10"),
        "limit_price": Decimal("190.50"),
    }
    fields.update(overrides)
    return BrokerOrderRequest(**fields)


def timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout("read timed out")


# ---------------------------------------------------------------- submit: happy path


def test_submit_sends_expected_request_to_paper_host_only() -> None:
    script = Script(jresp(200, order_json()))
    broker = make_broker(script)
    result = broker.submit_order(request())
    req = script.requests[0]
    assert req.method == "POST"
    assert req.url.host == "paper-api.alpaca.markets"
    assert req.url.scheme == "https"
    assert req.url.path == "/v2/orders"
    assert req.headers["APCA-API-KEY-ID"] == "PKTESTKEY123"
    assert req.headers["APCA-API-SECRET-KEY"] == "SECRETVALUE456"
    body = json.loads(req.content)
    assert body == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "client_order_id": CID,
        "limit_price": "190.50",
    }
    assert result.status is ExecutionStatus.ACCEPTED
    assert result.broker_order_id == "904837e3-3b76-47ec-b432-046db621571b"


def test_market_order_body_has_no_price_fields() -> None:
    script = Script(jresp(200, order_json(type="market", limit_price=None)))
    make_broker(script).submit_order(request(order_type=OrderType.MARKET, limit_price=None))
    body = json.loads(script.requests[0].content)
    assert "limit_price" not in body and "stop_price" not in body and body["type"] == "market"


def test_fill_money_is_decimal_and_exact() -> None:
    filled = order_json(
        status="filled",
        filled_qty="10",
        filled_avg_price="190.4999",
        filled_at="2026-09-20T10:00:01.000000001Z",
    )
    result = make_broker(Script(jresp(200, filled))).submit_order(request())
    assert result.status is ExecutionStatus.FILLED
    assert result.filled_avg_price == Decimal("190.4999")
    assert isinstance(result.filled_avg_price, Decimal)
    assert result.filled_quantity == Decimal("10")
    assert result.filled_at_ns is not None and result.filled_at_ns % 1_000_000_000 == 1


def test_numeric_json_money_is_parsed_without_float_noise() -> None:
    text = json.dumps(order_json(status="filled", filled_qty="10")).replace(
        '"filled_avg_price": null', '"filled_avg_price": 190.1'
    )
    result = make_broker(Script(httpx.Response(200, text=text))).submit_order(request())
    assert result.filled_avg_price == Decimal("190.1")


# ---------------------------------------------------------------- submit: definite reject


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_is_a_definitive_reject_and_never_retried(status: int) -> None:
    script = Script(jresp(status, {"code": 40010001, "message": "insufficient buying power"}))
    with pytest.raises(BrokerRejectedError) as exc:
        make_broker(script).submit_order(request())
    assert exc.value.http_status == status
    assert script.methods() == ["POST"]


def test_reject_message_is_truncated_and_carries_no_secrets() -> None:
    script = Script(jresp(403, {"message": "x" * 1000}))
    with pytest.raises(BrokerRejectedError) as exc:
        make_broker(script).submit_order(request())
    assert len(str(exc.value)) < 300
    assert "SECRETVALUE456" not in str(exc.value)


# ---------------------------------------------------------------- submit: ambiguous


def test_timeout_reconciles_by_client_order_id_and_reports_truth() -> None:
    script = Script(timeout(), jresp(200, order_json(status="filled", filled_qty="10", filled_avg_price="190.5")))
    result = make_broker(script).submit_order(request())
    assert script.methods() == ["POST", "GET"]  # POST exactly once: never blind-retried
    assert script.requests[1].url.path == "/v2/orders:by_client_order_id"
    assert script.requests[1].url.params["client_order_id"] == CID
    assert result.status is ExecutionStatus.FILLED


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 429])
def test_ambiguous_http_status_reconciles(status: int) -> None:
    script = Script(jresp(status, {"message": "boom"}), jresp(200, order_json()))
    result = make_broker(script).submit_order(request())
    assert script.methods() == ["POST", "GET"]
    assert result.status is ExecutionStatus.ACCEPTED


def test_ambiguous_then_not_found_fails_closed_never_resubmits() -> None:
    script = Script(jresp(503, {}), jresp(404, {"message": "order not found"}))
    with pytest.raises(SubmitOutcomeUnknown):
        make_broker(script).submit_order(request())
    assert script.methods().count("POST") == 1


def test_ambiguous_then_reconcile_unreachable_fails_closed() -> None:
    script = Script(timeout(), timeout(), timeout(), timeout())  # POST + 3 GET attempts
    sleeps: list[float] = []
    with pytest.raises(SubmitOutcomeUnknown):
        make_broker(script, sleeps=sleeps, retries=2).submit_order(request())
    assert script.methods() == ["POST", "GET", "GET", "GET"]
    assert len(sleeps) == 2


def test_malformed_2xx_body_reconciles() -> None:
    script = Script(httpx.Response(200, text="<html>gateway</html>"), jresp(200, order_json()))
    result = make_broker(script).submit_order(request())
    assert script.methods() == ["POST", "GET"]
    assert result.status is ExecutionStatus.ACCEPTED


def test_2xx_with_wrong_client_order_id_is_not_trusted() -> None:
    script = Script(jresp(200, order_json(client_order_id="someone-else")), jresp(404, {}))
    with pytest.raises(SubmitOutcomeUnknown):
        make_broker(script).submit_order(request())


def test_2xx_with_unparseable_money_is_ambiguous() -> None:
    script = Script(jresp(200, order_json(qty="ten")), jresp(404, {}))
    with pytest.raises(SubmitOutcomeUnknown):
        make_broker(script).submit_order(request())


def test_redirect_is_never_followed() -> None:
    script = Script(
        httpx.Response(307, headers={"location": "https://evil.example/v2/orders"}),
        jresp(404, {}),
    )
    with pytest.raises(SubmitOutcomeUnknown):
        make_broker(script).submit_order(request())
    assert {r.url.host for r in script.requests} == {"paper-api.alpaca.markets"}


# ---------------------------------------------------------------- safe reads retry


def test_get_order_retries_with_jittered_exponential_backoff() -> None:
    script = Script(jresp(503, {}), timeout(), jresp(200, order_json()))
    sleeps: list[float] = []
    found = make_broker(script, sleeps=sleeps).get_order_by_client_id(CID)
    assert found is not None and found.client_order_id == CID
    assert script.methods() == ["GET", "GET", "GET"]
    # full jitter: rng()=0.5 -> sleep = 0.5 * min(cap, base * 2**attempt)
    assert sleeps == [0.5 * 0.1, 0.5 * 0.2]


def test_jitter_is_bounded_by_cap() -> None:
    script = Script(*[jresp(503, {}) for _ in range(9)])
    sleeps: list[float] = []
    with pytest.raises(BrokerReadError):
        make_broker(script, sleeps=sleeps, retries=8).get_positions()
    assert max(sleeps) <= 0.5 * 2.0 + 1e-9


def test_get_order_not_found_returns_none_without_retry() -> None:
    script = Script(jresp(404, {"message": "not found"}))
    assert make_broker(script).get_order_by_client_id(CID) is None
    assert len(script.requests) == 1


def test_read_4xx_other_than_404_and_429_is_not_retried() -> None:
    script = Script(jresp(401, {"message": "unauthorized"}))
    with pytest.raises(BrokerReadError):
        make_broker(script).get_account()
    assert len(script.requests) == 1


def test_read_retries_exhausted_raises() -> None:
    script = Script(jresp(500, {}), jresp(500, {}), jresp(500, {}))
    with pytest.raises(BrokerReadError):
        make_broker(script, retries=2).get_account()
    assert len(script.requests) == 3


def test_get_positions_parses_decimals() -> None:
    body = [
        {"symbol": "AAPL", "qty": "10", "avg_entry_price": "190.25", "market_value": "1902.5", "side": "long"},
        {"symbol": "TSLA", "qty": "-3", "avg_entry_price": "250.10", "market_value": "-750.3", "side": "short"},
    ]
    positions = make_broker(Script(jresp(200, body))).get_positions()
    assert [p.symbol for p in positions] == ["AAPL", "TSLA"]
    assert positions[1].quantity == Decimal("-3")
    assert positions[0].avg_entry_price == Decimal("190.25")


def test_get_account_parses_decimals_and_flags() -> None:
    body = {
        "status": "ACTIVE",
        "buying_power": "100000.55",
        "cash": "50000",
        "equity": "50123.45",
        "trading_blocked": False,
        "account_blocked": False,
        "account_number": "PA123456789",
    }
    account = make_broker(Script(jresp(200, body))).get_account()
    assert account.buying_power == Decimal("100000.55")
    assert account.trading_blocked is False
    assert "PA123456789" not in repr(account)


def test_get_account_flags_blocked_accounts() -> None:
    body = {
        "status": "ACTIVE", "buying_power": "1", "cash": "1", "equity": "1",
        "trading_blocked": True, "account_blocked": False,
    }
    assert make_broker(Script(jresp(200, body))).get_account().trading_blocked is True


# ---------------------------------------------------------------- cancel


def test_cancel_is_never_retried() -> None:
    script = Script(jresp(500, {}))
    with pytest.raises(BrokerError):
        make_broker(script).cancel_order("904837e3-3b76-47ec-b432-046db621571b")
    assert script.methods() == ["DELETE"]


def test_cancel_success() -> None:
    script = Script(httpx.Response(204))
    make_broker(script).cancel_order("904837e3-3b76-47ec-b432-046db621571b")
    assert script.requests[0].url.path == "/v2/orders/904837e3-3b76-47ec-b432-046db621571b"


def test_cancel_rejects_path_traversal_ids() -> None:
    script = Script()
    with pytest.raises(BrokerError):
        make_broker(script).cancel_order("../account")
    assert script.requests == []


# ---------------------------------------------------------------- status mapping


@pytest.mark.parametrize(
    ("alpaca", "expected"),
    [
        ("new", ExecutionStatus.ACCEPTED),
        ("accepted", ExecutionStatus.ACCEPTED),
        ("pending_new", ExecutionStatus.ACCEPTED),
        ("pending_cancel", ExecutionStatus.ACCEPTED),
        ("partially_filled", ExecutionStatus.PARTIALLY_FILLED),
        ("filled", ExecutionStatus.FILLED),
        ("canceled", ExecutionStatus.CANCELLED),
        ("replaced", ExecutionStatus.CANCELLED),
        ("expired", ExecutionStatus.EXPIRED),
        ("done_for_day", ExecutionStatus.EXPIRED),
        ("rejected", ExecutionStatus.REJECTED),
        ("suspended", ExecutionStatus.REJECTED),
        ("stopped", ExecutionStatus.UNKNOWN),
        ("brand_new_status", ExecutionStatus.UNKNOWN),
    ],
)
def test_status_mapping_fails_closed_on_unknown(alpaca: str, expected: ExecutionStatus) -> None:
    result = make_broker(Script(jresp(200, order_json(status=alpaca)))).submit_order(request())
    assert result.status is expected
    assert result.raw_status == alpaca


# ---------------------------------------------------------------- hygiene


def test_secrets_are_not_logged() -> None:
    import structlog

    with structlog.testing.capture_logs() as logs:
        script = Script(timeout(), jresp(404, {}))
        with pytest.raises(SubmitOutcomeUnknown):
            make_broker(script).submit_order(request())
    assert logs, "expected the ambiguous-submit path to log"
    dump = json.dumps(logs, default=str)
    assert "PKTESTKEY123" not in dump and "SECRETVALUE456" not in dump
