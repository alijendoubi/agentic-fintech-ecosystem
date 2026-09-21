"""Finding 4: the market-order notional cap must not rely on a caller-supplied price."""

from __future__ import annotations

from decimal import Decimal

import pytest

from execution_motor.config import MotorConfig
from execution_motor.errors import ConfigError
from execution_motor.halt import KillSwitch
from execution_motor.models import AttestedOrder, ExecutionStatus, OrderType, RejectReason
from execution_motor.motor import ExecutionMotor
from execution_motor.quotes import PriceQuote
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy

from .helpers import NOW_NS, HmacTestVerifier, attest, make_order
from .test_motor import FakeBroker

D = Decimal
SEC = 1_000_000_000
_ENV = {"MOTOR_MAX_ORDER_NOTIONAL_USD": "1000", "MOTOR_MAX_SESSION_NOTIONAL_USD": "5000"}


class FakeQuotes:
    def __init__(self, quote: PriceQuote | Exception | None) -> None:
        self.quote = quote
        self.calls: list[str] = []

    def latest(self, symbol: str) -> PriceQuote | None:
        self.calls.append(symbol)
        if isinstance(self.quote, Exception):
            raise self.quote
        return self.quote


def _motor(
    broker: FakeBroker, quotes: FakeQuotes | None = None, kill: KillSwitch | None = None
) -> ExecutionMotor:
    return ExecutionMotor(
        config=MotorConfig(D("25000"), D("100000"), environment="test"),
        brokers={"alpaca-paper": broker},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=kill or KillSwitch(start_halted=False),
        verifier=HmacTestVerifier(),
        quotes=quotes,
        clock_ns=lambda: NOW_NS,
    )


def _market(qty: str = "10") -> AttestedOrder:
    return attest(make_order(order_type=OrderType.MARKET, limit_price=D("0"), quantity=D(qty)))


def _quote(price: str = "190", age_ns: int = SEC // 2) -> PriceQuote:
    return PriceQuote(price=D(price), as_of_ns=NOW_NS - age_ns)


def test_market_order_with_only_a_caller_price_is_rejected_and_never_submitted() -> None:
    broker = FakeBroker()
    report = _motor(broker).execute(_market("1000000"), reference_price=D("0.01"))
    assert report.reject_reason is RejectReason.NOTIONAL_UNDETERMINABLE
    assert broker.submitted == []


def test_understated_caller_price_cannot_lower_the_cap_price() -> None:
    broker = FakeBroker()
    motor = _motor(broker, FakeQuotes(_quote("190")))
    report = motor.execute(_market("200"), reference_price=D("1"))
    assert report.reject_reason is RejectReason.NOTIONAL_CAP_EXCEEDED  # 200 x 190 > 25,000
    assert broker.submitted == []


def test_market_order_within_cap_on_a_fresh_trusted_quote_is_submitted() -> None:
    broker = FakeBroker()
    quotes = FakeQuotes(_quote("190"))
    report = _motor(broker, quotes).execute(_market("10"))
    assert report.status is ExecutionStatus.FILLED
    assert quotes.calls == ["AAPL"]


@pytest.mark.parametrize(
    "quote",
    [
        None,
        PriceQuote(price=D("190"), as_of_ns=NOW_NS - 10 * SEC),  # stale
        PriceQuote(price=D("190"), as_of_ns=NOW_NS + 10 * SEC),  # from the future
        PriceQuote(price=D("0"), as_of_ns=NOW_NS),
        PriceQuote(price=D("-3"), as_of_ns=NOW_NS),
        PriceQuote(price=D("NaN"), as_of_ns=NOW_NS),
        PriceQuote(price=D("Infinity"), as_of_ns=NOW_NS),
    ],
)
def test_untrustworthy_quote_fails_closed(quote: PriceQuote | None) -> None:
    broker = FakeBroker()
    report = _motor(broker, FakeQuotes(quote)).execute(_market())
    assert report.reject_reason is RejectReason.NOTIONAL_UNDETERMINABLE
    assert broker.submitted == []


def test_failing_quote_source_rejects_without_halting_the_motor() -> None:
    kill = KillSwitch(start_halted=False)
    broker = FakeBroker()
    motor = _motor(broker, FakeQuotes(RuntimeError("feed down")), kill)
    report = motor.execute(_market())
    assert report.reject_reason is RejectReason.NOTIONAL_UNDETERMINABLE
    assert not kill.is_halted()
    assert broker.submitted == []


def test_limit_orders_do_not_need_a_quote_source() -> None:
    report = _motor(FakeBroker()).execute(attest(make_order()))
    assert report.status is ExecutionStatus.FILLED


def test_signed_stop_price_caps_a_stop_order_without_any_quote() -> None:
    stop = attest(
        make_order(
            order_type=OrderType.STOP,
            limit_price=D("0"),
            stop_price=D("300"),
            quantity=D("100"),
        )
    )
    report = _motor(FakeBroker()).execute(stop)
    assert report.reject_reason is RejectReason.NOTIONAL_CAP_EXCEEDED  # 100 x 300 > 25,000


def test_config_quote_age_defaults_and_validates() -> None:
    assert MotorConfig.from_env(_ENV).max_quote_age_ns == 2 * SEC
    overridden = MotorConfig.from_env({**_ENV, "MOTOR_MAX_QUOTE_AGE_MS": "250"})
    assert overridden.max_quote_age_ns == 250_000_000
    with pytest.raises(ConfigError):
        MotorConfig.from_env({**_ENV, "MOTOR_MAX_QUOTE_AGE_MS": "0"})
