from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

import pytest
from execution_motor.attestation import AttestationVerifier
from execution_motor.broker import (
    AccountSnapshot,
    Broker,
    BrokerOrder,
    BrokerOrderRequest,
    Position,
)
from execution_motor.config import MotorConfig
from execution_motor.errors import BrokerRejectedError, ConfigError, SubmitOutcomeUnknown
from execution_motor.halt import KillSwitch
from execution_motor.models import (
    Attestation,
    AttestedOrder,
    ExecAlgo,
    ExecutionReport,
    ExecutionStatus,
    OrderType,
    RejectReason,
    Side,
)
from execution_motor.motor import ExecutionMotor
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy
from execution_motor.toxicity import ToxicityParams, VenueObservation

from .helpers import NOW_NS, HmacTestVerifier, attest, make_order

D = Decimal
_VERIFIER = HmacTestVerifier()
SEC = 1_000_000_000


class FakeBroker(Broker):
    def __init__(self, name: str = "alpaca-paper", outcome: object = None) -> None:
        self._name = name
        self.outcome = outcome
        self.submitted: list[BrokerOrderRequest] = []

    @property
    def venue(self) -> str:
        return self._name

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        self.submitted.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if isinstance(self.outcome, BrokerOrder):
            return self.outcome
        return broker_order(request.client_order_id)

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return None

    def cancel_order(self, broker_order_id: str) -> None:
        return None

    def get_positions(self) -> tuple[Position, ...]:
        return ()

    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError


def broker_order(
    cid: str,
    status: ExecutionStatus = ExecutionStatus.FILLED,
    raw: str = "filled",
    filled: str = "10",
    price: str | None = "190.5",
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="b-1",
        client_order_id=cid,
        symbol="AAPL",
        status=status,
        raw_status=raw,
        quantity=D("10"),
        filled_quantity=D(filled),
        filled_avg_price=D(price) if price else None,
        submitted_at_ns=NOW_NS,
        filled_at_ns=NOW_NS + 1,
    )


def make_motor(
    broker: Broker | None = None,
    *,
    verifier: AttestationVerifier | None = _VERIFIER,
    halted: bool = False,
    order_cap: str = "25000",
    session_cap: str = "100000",
    unscored: UnscoredPolicy = UnscoredPolicy.ALLOW,
    brokers: Mapping[str, Broker] | None = None,
    stats: Callable[[], Mapping[str, Sequence[VenueObservation]]] | None = None,
) -> ExecutionMotor:
    fake = broker or FakeBroker()
    kill = KillSwitch(start_halted=halted)
    router = SmartOrderRouter(
        RouterConfig(params=ToxicityParams(min_observations=2), unscored_policy=unscored)
    )
    kwargs: dict[str, object] = {}
    if stats is not None:
        kwargs["venue_stats"] = stats
    return ExecutionMotor(
        config=MotorConfig(D(order_cap), D(session_cap)),
        brokers=brokers or {"alpaca-paper": fake},
        router=router,
        kill_switch=kill,
        verifier=verifier,
        clock_ns=lambda: NOW_NS,
        **kwargs,  # type: ignore[arg-type]
    )


def run(
    motor: ExecutionMotor, attested: AttestedOrder | None = None, **kw: object
) -> ExecutionReport:
    return motor.execute(attested or attest(make_order()), **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ happy path


def test_valid_order_is_submitted_once_and_reported() -> None:
    broker = FakeBroker()
    report = run(make_motor(broker))
    assert report.status is ExecutionStatus.FILLED
    assert report.venue == "alpaca-paper"
    assert report.filled_quantity == D("10") and report.avg_fill_price == D("190.5")
    assert report.broker_order_id == "b-1"
    assert len(broker.submitted) == 1
    sent = broker.submitted[0]
    assert sent.client_order_id == make_order().order_id
    assert (sent.symbol, sent.side, sent.quantity, sent.limit_price) == (
        "AAPL",
        Side.BUY,
        D("10"),
        D("190.50"),
    )
    assert sent.stop_price is None


def test_partial_and_accepted_statuses_are_reported() -> None:
    partial = FakeBroker(
        outcome=broker_order("x", ExecutionStatus.PARTIALLY_FILLED, "partially_filled", "4")
    )
    assert run(make_motor(partial)).status is ExecutionStatus.PARTIALLY_FILLED
    accepted = FakeBroker(outcome=broker_order("x", ExecutionStatus.ACCEPTED, "new", "0", None))
    report = run(make_motor(accepted))
    assert report.status is ExecutionStatus.ACCEPTED and report.fills == ()


def test_slippage_uses_reference_price() -> None:
    report = run(make_motor(), reference_price=D("190.00"))
    assert report.arrival_price == D("190.00")
    assert report.slippage_bps is not None and report.slippage_bps > 0


# ------------------------------------------------------------- attestation gate


def test_default_motor_denies_everything_and_never_calls_broker() -> None:
    broker = FakeBroker()
    report = run(make_motor(broker, verifier=None))
    assert report.reject_reason is RejectReason.ATTESTATION_INVALID
    assert broker.submitted == []


def _mutated(att: AttestedOrder, **changes: object) -> AttestedOrder:
    return att.model_copy(update={"attestation": att.attestation.model_copy(update=changes)})


def test_missing_attestation_is_rejected() -> None:
    broker = FakeBroker()
    bad = _mutated(attest(make_order()), signature=b"")
    assert run(make_motor(broker), bad).reject_reason is RejectReason.ATTESTATION_MISSING
    assert broker.submitted == []


def test_invalid_signature_is_rejected() -> None:
    broker = FakeBroker()
    bad = _mutated(attest(make_order()), signature=b"\x01" * 32)
    assert run(make_motor(broker), bad).reject_reason is RejectReason.ATTESTATION_INVALID
    assert broker.submitted == []


def test_forged_with_wrong_secret_is_rejected() -> None:
    forged = attest(make_order(), secret=b"attacker")
    assert run(make_motor(), forged).reject_reason is RejectReason.ATTESTATION_INVALID


def test_expired_attestation_is_rejected() -> None:
    old = attest(make_order(), decided_at_ns=NOW_NS - 10 * SEC, expires_at_ns=NOW_NS - SEC)
    assert run(make_motor(), old).reject_reason is RejectReason.ORDER_EXPIRED


def test_expiry_boundary_is_exclusive() -> None:
    edge = attest(make_order(), expires_at_ns=NOW_NS)
    assert run(make_motor(), edge).reject_reason is RejectReason.ORDER_EXPIRED


def test_attestation_older_than_max_age_is_rejected_even_if_not_yet_expired() -> None:
    stale = attest(make_order(), decided_at_ns=NOW_NS - 6 * SEC, expires_at_ns=NOW_NS + 60 * SEC)
    assert run(make_motor(), stale).reject_reason is RejectReason.ORDER_EXPIRED


def test_future_dated_attestation_is_rejected() -> None:
    future = attest(make_order(), decided_at_ns=NOW_NS + 5 * SEC, expires_at_ns=NOW_NS + 9 * SEC)
    assert run(make_motor(), future).reject_reason is RejectReason.ORDER_FROM_FUTURE


def test_unsigned_created_at_is_not_trusted_for_expiry() -> None:
    ancient_created = attest(make_order(created_at_ns=1))
    assert run(make_motor(), ancient_created).status is ExecutionStatus.FILLED


def test_default_expiry_absent_means_expired() -> None:
    absent = _mutated(attest(make_order()), expires_at_ns=0)
    assert run(make_motor(), absent).reject_reason is RejectReason.ORDER_EXPIRED


# ---------------------------------------------------------------------- replay


def test_replayed_order_is_duplicate_and_broker_called_once() -> None:
    broker = FakeBroker()
    motor = make_motor(broker)
    first = run(motor)
    second = run(motor)
    assert first.status is ExecutionStatus.FILLED
    assert second.reject_reason is RejectReason.DUPLICATE_ORDER
    assert len(broker.submitted) == 1


def test_relabelled_order_id_is_refused_order_id_must_equal_signal_id() -> None:
    broker = FakeBroker()
    motor = make_motor(broker)
    run(motor)
    relabelled = attest(make_order(order_id="another-order-id", signal_id=make_order().signal_id))
    assert run(motor, relabelled).reject_reason is RejectReason.ORDER_ID_MISMATCH
    assert len(broker.submitted) == 1


def test_order_id_mismatch_is_refused_even_for_a_fresh_signal() -> None:
    broker = FakeBroker()
    order = make_order(order_id="order-x", signal_id="signal-y")
    report = run(make_motor(broker), attest(order))
    assert report.reject_reason is RejectReason.ORDER_ID_MISMATCH
    assert broker.submitted == []


def test_invalid_attestation_does_not_burn_idempotency_keys() -> None:
    motor = make_motor()
    run(motor, _mutated(attest(make_order()), signature=b"\x02" * 32))
    assert run(motor).status is ExecutionStatus.FILLED


# ----------------------------------------------------------------- notional caps


def test_per_order_cap_enforced() -> None:
    big = attest(make_order(quantity=D("200"), limit_price=D("190.50")))  # 38,100 > 25,000
    broker = FakeBroker()
    assert run(make_motor(broker), big).reject_reason is RejectReason.NOTIONAL_CAP_EXCEEDED
    assert broker.submitted == []


def test_cap_boundary_is_inclusive() -> None:
    exact = attest(make_order(quantity=D("100"), limit_price=D("250")))  # exactly 25,000
    assert run(make_motor(), exact).status is ExecutionStatus.FILLED


def test_market_order_without_reference_price_is_rejected() -> None:
    market = attest(make_order(order_type=OrderType.MARKET, limit_price=D("0")))
    assert run(make_motor(), market).reject_reason is RejectReason.NOTIONAL_UNDETERMINABLE


def test_market_order_capped_using_reference_price() -> None:
    market = attest(make_order(order_type=OrderType.MARKET, limit_price=D("0"), quantity=D("200")))
    over = run(make_motor(), market, reference_price=D("190"))
    assert over.reject_reason is RejectReason.NOTIONAL_CAP_EXCEEDED
    ok_order = attest(make_order(order_type=OrderType.MARKET, limit_price=D("0"), quantity=D("10")))
    assert run(make_motor(), ok_order, reference_price=D("190")).status is ExecutionStatus.FILLED


def test_session_cap_accumulates_and_broker_reject_releases_capacity() -> None:
    motor = make_motor(order_cap="20000", session_cap="30000")
    a = attest(make_order(order_id="a", quantity=D("100"), limit_price=D("190")))
    b = attest(make_order(order_id="b", quantity=D("100"), limit_price=D("190")))
    assert run(motor, a).status is ExecutionStatus.FILLED  # 19,000 reserved
    assert run(motor, b).reject_reason is RejectReason.SESSION_NOTIONAL_CAP_EXCEEDED

    rejecting = FakeBroker(outcome=BrokerRejectedError("nope", http_status=403))
    motor2 = make_motor(rejecting, order_cap="20000", session_cap="20000")
    c = attest(make_order(order_id="c", quantity=D("100"), limit_price=D("190")))
    d = attest(make_order(order_id="d", quantity=D("100"), limit_price=D("190")))
    assert run(motor2, c).reject_reason is RejectReason.BROKER_REJECTED
    rejecting.outcome = None
    assert run(motor2, d).status is ExecutionStatus.FILLED  # capacity was released


def test_config_requires_session_cap_at_least_order_cap() -> None:
    with pytest.raises(ConfigError):
        MotorConfig(D("100"), D("50"))


# ------------------------------------------------------------------------ halt


def test_halted_motor_never_submits() -> None:
    broker = FakeBroker()
    report = run(make_motor(broker, halted=True))
    assert report.reject_reason is RejectReason.HALTED
    assert broker.submitted == []


def test_default_kill_switch_state_is_halted() -> None:
    assert KillSwitch().is_halted()


def test_halt_takes_effect_between_orders() -> None:
    broker = FakeBroker()
    kill = KillSwitch(start_halted=False)
    motor = ExecutionMotor(
        config=MotorConfig(D("25000"), D("100000")),
        brokers={"alpaca-paper": broker},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=kill,
        verifier=HmacTestVerifier(),
        clock_ns=lambda: NOW_NS,
    )
    assert run(motor).status is ExecutionStatus.FILLED
    kill.halt("operator stop")
    other = attest(make_order(order_id="o2"))
    assert run(motor, other).reject_reason is RejectReason.HALTED
    assert len(broker.submitted) == 1


def test_halt_flipped_by_router_stats_callback_is_caught_before_submit() -> None:
    broker = FakeBroker()
    kill = KillSwitch(start_halted=False)

    def stats() -> Mapping[str, Sequence[VenueObservation]]:
        kill.halt("halted mid-flight")  # halt lands after the first check, before the submit
        return {}

    motor = ExecutionMotor(
        config=MotorConfig(D("25000"), D("100000")),
        brokers={"alpaca-paper": broker},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=kill,
        verifier=HmacTestVerifier(),
        venue_stats=stats,
        clock_ns=lambda: NOW_NS,
    )
    assert run(motor).reject_reason is RejectReason.HALTED
    assert broker.submitted == []


# ----------------------------------------------------- ambiguity / errors halt


def test_unknown_outcome_reports_unknown_halts_and_blocks_resubmission() -> None:
    broker = FakeBroker(outcome=SubmitOutcomeUnknown("timeout; not found"))
    motor = make_motor(broker)
    report = run(motor)
    assert report.status is ExecutionStatus.UNKNOWN
    assert report.reject_reason is RejectReason.SUBMIT_OUTCOME_UNKNOWN
    again = run(motor)
    assert again.reject_reason is RejectReason.HALTED
    assert len(broker.submitted) == 1


def test_unexpected_broker_exception_is_unknown_and_halts() -> None:
    broker = FakeBroker(outcome=RuntimeError("kaboom"))
    motor = make_motor(broker)
    report = run(motor)
    assert report.status is ExecutionStatus.UNKNOWN
    assert run(motor, attest(make_order(order_id="o2"))).reject_reason is RejectReason.HALTED


def test_unmapped_broker_status_is_unknown_and_halts() -> None:
    broker = FakeBroker(outcome=broker_order("x", ExecutionStatus.UNKNOWN, "stopped", "0", None))
    assert run(make_motor(broker)).status is ExecutionStatus.UNKNOWN


def test_broker_rejected_status_maps_to_rejected() -> None:
    broker = FakeBroker(outcome=broker_order("x", ExecutionStatus.REJECTED, "rejected", "0", None))
    report = run(make_motor(broker))
    assert (
        report.status is ExecutionStatus.REJECTED
        and report.reject_reason is RejectReason.BROKER_REJECTED
    )


def test_definite_broker_reject_does_not_halt() -> None:
    motor = make_motor(FakeBroker(outcome=BrokerRejectedError("insufficient", http_status=403)))
    assert run(motor).reject_reason is RejectReason.BROKER_REJECTED
    other = attest(make_order(order_id="o2"))
    assert (
        run(motor, other).reject_reason is RejectReason.BROKER_REJECTED
    )  # still trading, not halted


def test_internal_error_before_submit_halts_and_rejects() -> None:
    def boom() -> Mapping[str, Sequence[VenueObservation]]:
        raise RuntimeError("stats backend down")

    broker = FakeBroker()
    motor = make_motor(broker, stats=boom)
    report = run(motor)
    assert report.reject_reason is RejectReason.INTERNAL_ERROR
    assert broker.submitted == []
    assert run(motor, attest(make_order(order_id="o2"))).reject_reason is RejectReason.HALTED


# ------------------------------------------------------------------ algo / SOR


def test_unsupported_algo_rejected() -> None:
    iceberg = attest(make_order(algo=ExecAlgo.ICEBERG))
    assert run(make_motor(), iceberg).reject_reason is RejectReason.ALGO_UNSUPPORTED


def test_unscored_venue_denied_by_default_router() -> None:
    broker = FakeBroker()
    report = run(make_motor(broker, unscored=UnscoredPolicy.DENY))
    assert report.reject_reason is RejectReason.NO_ELIGIBLE_VENUE
    assert broker.submitted == []


def _obs(fill: str, after: str, filled: str) -> VenueObservation:
    return VenueObservation(
        timestamp_ns=NOW_NS - 1000,
        side=Side.BUY,
        ordered_qty=D("100"),
        filled_qty=D(filled),
        avg_fill_price=D(fill),
        arrival_mid=D("100"),
        mid_after_horizon=D(after),
    )


def test_motor_routes_around_toxic_venue() -> None:
    toxic, clean = FakeBroker("toxic-venue"), FakeBroker("clean-venue")
    stats = {
        "toxic-venue": [_obs("101", "99", "50")] * 2,
        "clean-venue": [_obs("100", "100.1", "100")] * 2,
    }
    motor = make_motor(brokers={"toxic-venue": toxic, "clean-venue": clean}, stats=lambda: stats)
    report = run(motor)
    assert report.venue == "clean-venue"
    assert toxic.submitted == [] and len(clean.submitted) == 1


def test_motor_requires_at_least_one_broker() -> None:
    with pytest.raises(ConfigError):
        ExecutionMotor(
            config=MotorConfig(D("1"), D("1")),
            brokers={},
            router=SmartOrderRouter(RouterConfig()),
            kill_switch=KillSwitch(),
        )


def test_execute_never_raises_on_hostile_attestation_types() -> None:
    weird = AttestedOrder(
        order=make_order(),
        attestation=Attestation(signature=b"s", key_id="k", signed_payload=b"p"),
    )
    assert run(make_motor(), weird).status is ExecutionStatus.REJECTED
