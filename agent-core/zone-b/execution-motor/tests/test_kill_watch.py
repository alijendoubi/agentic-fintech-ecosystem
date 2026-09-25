"""ALI-162: execution-motor reacts to Aegis kill-switch state (spec phase_3 §5.2).

* LOGIC (2) and above: cancel every open broker order, issued within 1 s of the latch.
* A missing / broken state stream is treated as HARD, never NORMAL (aegis.proto risk note 1).
* The motor mirrors Aegis's level; Aegis owns latching and the authenticated reset.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

import pytest

from execution_motor.broker import (
    AccountSnapshot,
    Broker,
    BrokerOrder,
    BrokerOrderRequest,
    Position,
)
from execution_motor.errors import BrokerError, BrokerReadError
from execution_motor.halt import KillStateHaltSource, KillSwitch
from execution_motor.kill_watch import (
    KILL_LEVEL_HARD,
    KILL_LEVEL_LOGIC,
    KILL_LEVEL_NORMAL,
    KILL_LEVEL_SOFT,
    KillLevelMirror,
    KillSwitchWatcher,
    OpenOrderCanceller,
)
from execution_motor.models import ExecutionStatus


@dataclass(frozen=True)
class State:
    """Stand-in for aegis_pb2.KillSwitchState (only the fields the watcher reads)."""

    effective_level: int
    state_seq: int


def open_order(broker_id: str) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_id,
        client_order_id=f"c-{broker_id}",
        symbol="AAPL",
        status=ExecutionStatus.ACCEPTED,
        raw_status="new",
        quantity=Decimal("10"),
        filled_quantity=Decimal("0"),
    )


class OpenOrdersBroker(Broker):
    def __init__(
        self,
        venue: str = "alpaca-paper",
        open_ids: tuple[str, ...] = (),
        *,
        list_error: Exception | None = None,
        cancel_errors: frozenset[str] = frozenset(),
    ) -> None:
        self._venue = venue
        self.open_ids = list(open_ids)
        self.list_error = list_error
        self.cancel_errors = cancel_errors
        self.cancelled: list[str] = []
        self.list_calls = 0

    @property
    def venue(self) -> str:
        return self._venue

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        raise AssertionError("the watcher must never submit")

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return None

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return tuple(open_order(i) for i in self.open_ids)

    def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id in self.cancel_errors:
            raise BrokerError("cancel failed: HTTP 500")
        self.cancelled.append(broker_order_id)
        self.open_ids.remove(broker_order_id)

    def get_positions(self) -> tuple[Position, ...]:
        return ()

    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError


def make_watcher(
    *brokers: OpenOrdersBroker, stream: Iterator[State] | None = None
) -> tuple[KillSwitchWatcher, KillLevelMirror]:
    mirror = KillLevelMirror()
    canceller = OpenOrderCanceller({b.venue: b for b in brokers})
    watcher = KillSwitchWatcher(
        open_stream=lambda: iter(stream or ()),
        mirror=mirror,
        canceller=canceller,
        resweep_interval_s=0.05,
        reconnect_backoff_s=0.01,
    )
    return watcher, mirror


# ---------------------------------------------------------------- mirror


def test_mirror_starts_unknown_and_reports_hard() -> None:
    mirror = KillLevelMirror()
    assert not mirror.is_known()
    assert mirror.level() == KILL_LEVEL_HARD


def test_mirror_follows_aegis_down_after_an_aegis_reset() -> None:
    mirror = KillLevelMirror()
    mirror.apply(KILL_LEVEL_LOGIC)
    mirror.apply(KILL_LEVEL_NORMAL)
    assert mirror.is_known() and mirror.level() == KILL_LEVEL_NORMAL


@pytest.mark.parametrize("bogus", [-1, 6, 99])
def test_mirror_treats_an_out_of_range_level_as_hard(bogus: int) -> None:
    mirror = KillLevelMirror()
    mirror.apply(bogus)
    assert mirror.level() == KILL_LEVEL_HARD


def test_mirror_unknown_after_stream_loss_is_hard() -> None:
    mirror = KillLevelMirror()
    mirror.apply(KILL_LEVEL_NORMAL)
    mirror.mark_unknown()
    assert not mirror.is_known() and mirror.level() == KILL_LEVEL_HARD


# ---------------------------------------------------------------- halting new submissions


def test_non_latching_switch_follows_the_aegis_level_both_ways() -> None:
    mirror = KillLevelMirror()
    kill = KillSwitch(
        start_halted=False,
        signal_source=KillStateHaltSource(mirror.level),
        latch_external=False,
    )
    assert kill.is_halted()  # unknown => HARD
    mirror.apply(KILL_LEVEL_NORMAL)
    assert not kill.is_halted()
    mirror.apply(KILL_LEVEL_SOFT)
    assert kill.is_halted()
    mirror.apply(KILL_LEVEL_NORMAL)  # Aegis reset, authenticated there
    assert not kill.is_halted()


def test_non_latching_switch_still_honours_a_local_halt() -> None:
    mirror = KillLevelMirror()
    mirror.apply(KILL_LEVEL_NORMAL)
    kill = KillSwitch(
        start_halted=False,
        signal_source=KillStateHaltSource(mirror.level),
        latch_external=False,
    )
    kill.halt("operator")
    assert kill.is_halted()


def test_non_latching_switch_halts_while_the_source_raises_but_does_not_latch() -> None:
    fail = {"on": True}

    def level() -> int:
        if fail["on"]:
            raise RuntimeError("boom")
        return KILL_LEVEL_NORMAL

    kill = KillSwitch(
        start_halted=False, signal_source=KillStateHaltSource(level), latch_external=False
    )
    assert kill.is_halted()
    fail["on"] = False
    assert not kill.is_halted()


def test_default_switch_keeps_its_sticky_behaviour() -> None:
    mirror = KillLevelMirror()
    mirror.apply(KILL_LEVEL_LOGIC)
    kill = KillSwitch(start_halted=False, signal_source=KillStateHaltSource(mirror.level))
    assert kill.is_halted()
    mirror.apply(KILL_LEVEL_NORMAL)
    assert kill.is_halted()  # sticky until explicit resume


# ---------------------------------------------------------------- canceller


def test_sweep_cancels_every_open_order_on_every_venue() -> None:
    a = OpenOrdersBroker("alpaca-paper", ("o1", "o2"))
    b = OpenOrdersBroker("other", ("o3",))
    result = OpenOrderCanceller({"alpaca-paper": a, "other": b}).sweep("LOGIC")

    assert a.cancelled == ["o1", "o2"] and b.cancelled == ["o3"]
    assert result.listed == 3
    assert result.cancelled == ("o1", "o2", "o3")
    assert result.complete


def test_sweep_keeps_going_past_a_failed_cancel_and_reports_it() -> None:
    broker = OpenOrdersBroker(open_ids=("o1", "o2", "o3"), cancel_errors=frozenset({"o2"}))
    result = OpenOrderCanceller({broker.venue: broker}).sweep("LOGIC")

    assert broker.cancelled == ["o1", "o3"]
    assert result.failed == ("o2",)
    assert not result.complete


def test_sweep_keeps_going_past_an_unreadable_venue_and_reports_it() -> None:
    bad = OpenOrdersBroker("bad", list_error=BrokerReadError("read failed: HTTP 500"))
    good = OpenOrdersBroker("good", ("o1",))
    result = OpenOrderCanceller({"bad": bad, "good": good}).sweep("LOGIC")

    assert good.cancelled == ["o1"]
    assert result.unreadable_venues == ("bad",)
    assert not result.complete


def test_sweep_survives_an_unexpected_exception_from_a_broker() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",), list_error=RuntimeError("bug"))
    result = OpenOrderCanceller({broker.venue: broker}).sweep("LOGIC")
    assert result.unreadable_venues == (broker.venue,)


# ---------------------------------------------------------------- watcher reactions


def test_transition_to_logic_cancels_open_orders() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    watcher, mirror = make_watcher(broker)
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 1))
    assert broker.cancelled == []

    watcher.handle_state(State(KILL_LEVEL_LOGIC, 2))
    assert mirror.level() == KILL_LEVEL_LOGIC
    assert broker.cancelled == ["o1"]


def test_soft_does_not_touch_open_orders() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    watcher, _ = make_watcher(broker)
    watcher.handle_state(State(KILL_LEVEL_SOFT, 1))
    assert broker.cancelled == [] and broker.list_calls == 0


def test_hard_also_cancels_while_egress_may_still_be_open() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    watcher, _ = make_watcher(broker)
    watcher.handle_state(State(KILL_LEVEL_HARD, 1))
    assert broker.cancelled == ["o1"]


def test_stream_loss_is_treated_as_hard_and_cancels() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    watcher, mirror = make_watcher(broker)
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 1))
    watcher.handle_stream_down("UNAVAILABLE")
    assert mirror.level() == KILL_LEVEL_HARD
    assert broker.cancelled == ["o1"]


def test_an_older_state_on_the_same_stream_is_ignored() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    watcher, mirror = make_watcher(broker)
    watcher.begin_stream()
    watcher.handle_state(State(KILL_LEVEL_LOGIC, 5))
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 4))  # stale, out of order
    assert mirror.level() == KILL_LEVEL_LOGIC


def test_a_new_stream_accepts_its_first_state_whatever_the_seq() -> None:
    watcher, mirror = make_watcher(OpenOrdersBroker())
    watcher.begin_stream()
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 50))
    watcher.handle_stream_down("reconnect")
    watcher.begin_stream()
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 3))  # Aegis state store may restart seq
    assert mirror.is_known() and mirror.level() == KILL_LEVEL_NORMAL


def test_consume_marks_unknown_when_the_stream_ends() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    watcher, mirror = make_watcher(broker)
    watcher.consume(iter([State(KILL_LEVEL_NORMAL, 1)]))
    assert mirror.level() == KILL_LEVEL_HARD
    assert broker.cancelled == ["o1"]


def test_consume_marks_unknown_when_the_stream_raises() -> None:
    def broken() -> Iterator[State]:
        yield State(KILL_LEVEL_NORMAL, 1)
        raise RuntimeError("UNAVAILABLE")

    watcher, mirror = make_watcher(OpenOrdersBroker())
    watcher.consume(broken())
    assert not mirror.is_known()


def test_resweep_catches_orders_that_appear_while_elevated() -> None:
    broker = OpenOrdersBroker()
    watcher, _ = make_watcher(broker)
    watcher.handle_state(State(KILL_LEVEL_LOGIC, 1))
    broker.open_ids.append("late")  # an in-flight submit landed after the first sweep
    watcher.sweep_if_elevated()
    assert broker.cancelled == ["late"]


def test_no_sweep_at_normal() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    watcher, _ = make_watcher(broker)
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 1))
    assert watcher.sweep_if_elevated() is None
    assert broker.list_calls == 0


# ---------------------------------------------------------------- threads


def test_background_watcher_cancels_within_one_second_of_a_logic_trip() -> None:
    broker = OpenOrdersBroker(open_ids=("o1",))
    tripped = threading.Event()
    release = threading.Event()

    def stream() -> Iterator[State]:
        yield State(KILL_LEVEL_NORMAL, 1)
        tripped.wait(timeout=5)
        yield State(KILL_LEVEL_LOGIC, 2)
        release.wait(timeout=5)

    mirror = KillLevelMirror()
    watcher = KillSwitchWatcher(
        open_stream=stream,
        mirror=mirror,
        canceller=OpenOrderCanceller({broker.venue: broker}),
        resweep_interval_s=10.0,  # the trip must be handled by the wake-up, not the timer
        reconnect_backoff_s=0.01,
    )
    watcher.start()
    try:
        deadline = time.monotonic() + 2
        while not mirror.is_known() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert mirror.level() == KILL_LEVEL_NORMAL
        started = time.monotonic()
        tripped.set()
        while not broker.cancelled and time.monotonic() - started < 1.0:
            time.sleep(0.005)
        assert broker.cancelled == ["o1"]
    finally:
        release.set()
        watcher.stop()


def test_stop_cancels_the_live_grpc_call() -> None:
    class Call:
        def __init__(self) -> None:
            self.cancelled = threading.Event()

        def __iter__(self) -> Iterator[State]:
            yield State(KILL_LEVEL_NORMAL, 1)
            self.cancelled.wait(timeout=5)

        def cancel(self) -> None:
            self.cancelled.set()

    call = Call()
    mirror = KillLevelMirror()
    watcher = KillSwitchWatcher(
        open_stream=lambda: call,
        mirror=mirror,
        canceller=OpenOrderCanceller({}),
        resweep_interval_s=10.0,
        reconnect_backoff_s=10.0,
    )
    watcher.start()
    deadline = time.monotonic() + 2
    while not mirror.is_known() and time.monotonic() < deadline:
        time.sleep(0.005)
    watcher.stop()
    assert call.cancelled.is_set()
    assert not watcher.is_running()


def test_aegis_unreachable_at_start_up_cancels_open_orders_at_once() -> None:
    # Review finding: a motor restarting with orders open while Aegis is down must not
    # wait for the periodic timer.
    broker = OpenOrdersBroker(open_ids=("o1",))

    def unreachable() -> Iterator[State]:
        raise ConnectionError("aegis unreachable")

    watcher = KillSwitchWatcher(
        open_stream=unreachable,
        mirror=KillLevelMirror(),
        canceller=OpenOrderCanceller({broker.venue: broker}),
        resweep_interval_s=30.0,
        reconnect_backoff_s=30.0,
    )
    watcher.start()
    try:
        started = time.monotonic()
        while not broker.cancelled and time.monotonic() - started < 1.0:
            time.sleep(0.005)
        assert broker.cancelled == ["o1"]
    finally:
        watcher.stop()


class SlowBroker(OpenOrdersBroker):
    def __init__(self) -> None:
        super().__init__(open_ids=("o1",))
        self.entered = threading.Event()
        self.release = threading.Event()

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        self.entered.set()
        self.release.wait(timeout=5)
        return super().list_open_orders()


def test_a_slow_broker_does_not_stop_the_stream_thread_seeing_a_reset() -> None:
    # Review finding: the sweep must not run on the stream thread.
    broker = SlowBroker()
    done = threading.Event()

    def stream() -> Iterator[State]:
        yield State(KILL_LEVEL_LOGIC, 1)
        broker.entered.wait(timeout=5)
        yield State(KILL_LEVEL_NORMAL, 2)
        done.wait(timeout=5)

    mirror = KillLevelMirror()
    watcher = KillSwitchWatcher(
        open_stream=stream,
        mirror=mirror,
        canceller=OpenOrderCanceller({broker.venue: broker}),
        resweep_interval_s=30.0,
        reconnect_backoff_s=30.0,
    )
    watcher.start()
    try:
        assert broker.entered.wait(timeout=2)
        deadline = time.monotonic() + 2
        while mirror.level() != KILL_LEVEL_NORMAL and time.monotonic() < deadline:
            time.sleep(0.005)
        assert mirror.level() == KILL_LEVEL_NORMAL  # seen while the sweep is still blocked
        assert broker.cancelled == []
        broker.release.set()
        deadline = time.monotonic() + 2
        while not broker.cancelled and time.monotonic() < deadline:
            time.sleep(0.005)
        assert broker.cancelled == ["o1"]  # the LOGIC-requested sweep still completes
    finally:
        broker.release.set()
        done.set()
        watcher.stop()


# ---------------------------------------------------------------- liveness probe (ALI-170)


def test_failed_probe_marks_state_unknown_sweeps_and_cancels_the_stream() -> None:
    """ALI-170 drill: keepalive did not break the idle stream to a frozen Aegis. A failed
    unary probe must stand in for it."""
    broker = OpenOrdersBroker(open_ids=("o1",))

    def frozen() -> None:
        raise TimeoutError("deadline exceeded")

    class Call:
        cancelled = False

        def cancel(self) -> None:
            Call.cancelled = True

    mirror = KillLevelMirror()
    watcher = KillSwitchWatcher(
        open_stream=lambda: iter(()),
        mirror=mirror,
        canceller=OpenOrderCanceller({broker.venue: broker}),
        probe=frozen,
    )
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 1))
    watcher._call = Call()  # the live stream the stream thread is blocked on
    assert watcher.check_liveness() is False
    assert not mirror.is_known() and mirror.level() == KILL_LEVEL_HARD
    assert broker.cancelled == ["o1"]
    assert Call.cancelled, "the stuck stream must be cancelled so it reconnects"


def test_probe_is_skipped_while_state_is_unknown_and_passes_when_healthy() -> None:
    calls: list[int] = []
    watcher, mirror = make_watcher(OpenOrdersBroker())
    watcher._probe = lambda: calls.append(1)
    assert watcher.check_liveness() is True and calls == [], "unknown: nothing to confirm"
    watcher.handle_state(State(KILL_LEVEL_NORMAL, 1))
    assert watcher.check_liveness() is True and calls == [1]
    assert mirror.level() == KILL_LEVEL_NORMAL


def test_background_probe_detects_a_frozen_peer_within_one_interval() -> None:
    frozen = threading.Event()
    release = threading.Event()

    def stream() -> Iterator[State]:
        yield State(KILL_LEVEL_NORMAL, 1)
        release.wait(timeout=10)  # a frozen peer: the stream just stops, no error

    def probe() -> None:
        if frozen.is_set():
            raise TimeoutError("deadline exceeded")

    mirror = KillLevelMirror()
    watcher = KillSwitchWatcher(
        open_stream=stream,
        mirror=mirror,
        canceller=OpenOrderCanceller({}),
        resweep_interval_s=0.1,
        reconnect_backoff_s=10.0,
        probe=probe,
    )
    watcher.start()
    try:
        deadline = time.monotonic() + 2
        while not mirror.is_known() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mirror.level() == KILL_LEVEL_NORMAL
        frozen.set()
        deadline = time.monotonic() + 2
        while mirror.is_known() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert mirror.level() == KILL_LEVEL_HARD
    finally:
        release.set()
        watcher.stop()
