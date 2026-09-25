"""React to Aegis kill-switch state (ALI-162, spec ``phase_3_aegis_execution.md`` §5.2).

Aegis is the latching authority: it owns the triggers, the effective level and the
authenticated reset. The motor subscribes to ``Aegis.WatchKillSwitchState`` and:

* mirrors the effective level (``KillLevelMirror``) so ``KillSwitch`` halts new submissions
  at any level above NORMAL and resumes when Aegis resets (no motor-side latch);
* at LOGIC (2) and above cancels every open broker order (``OpenOrderCanceller``). Each state
  message wakes the sweeper thread at once, well inside the spec's 1 s budget, and the
  sweep repeats every ``resweep_interval_s`` while the level stays elevated (orders from
  in-flight submits, failed cancels, and pages beyond the first are caught on a later
  pass). Sweeps run off the stream thread, so a slow broker never delays reading the next
  state;
* treats a missing, broken or ended stream as HARD, never NORMAL (``aegis.proto`` design
  risk note 1: a proto3 default decodes as NORMAL, so absence must not be trusted).

Fail-closed consequence, on purpose: until the first state arrives the level is HARD, so
new submissions are halted. Every failed subscribe or broken stream, including the first
attempt at start-up, triggers an immediate sweep, so a motor restarting with orders open
while Aegis is unreachable cancels them at once. A normal restart with Aegis healthy does
not sweep, because the first state arrives before the periodic timer fires. SOFT (1) halts
new submissions but leaves open orders untouched, as the spec says. At HARD the supervisor
may already have cut broker egress; the sweep still tries, and failures are logged at
CRITICAL for manual cancellation per ``docs/runbooks/kill-switch-drill.md``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import structlog

from .broker import Broker
from .halt import KillStateHaltSource, KillSwitch

KILL_LEVEL_NORMAL: Final = 0
KILL_LEVEL_SOFT: Final = 1
KILL_LEVEL_LOGIC: Final = 2
KILL_LEVEL_HARD: Final = 4
_KILL_LEVEL_MAX: Final = 5  # PHYSICAL
CANCEL_THRESHOLD: Final = KILL_LEVEL_LOGIC

_log = structlog.get_logger("execution_motor.kill_watch")


class KillLevelMirror:
    """Latest effective level reported by Aegis. Unknown (the initial state) reads as HARD."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._level = KILL_LEVEL_HARD
        self._known = False

    def level(self) -> int:
        with self._lock:
            return self._level

    def is_known(self) -> bool:
        with self._lock:
            return self._known

    def apply(self, level: int) -> int:
        """Record a level reported by Aegis. Returns the previous level."""
        safe = level if KILL_LEVEL_NORMAL <= level <= _KILL_LEVEL_MAX else KILL_LEVEL_HARD
        if safe != level:
            _log.error("kill_level_out_of_range", received=level, treated_as=safe)
        with self._lock:
            previous, self._level, self._known = self._level, safe, True
        return previous

    def mark_unknown(self) -> int:
        """No trustworthy state (stream down). Returns the previous level."""
        with self._lock:
            previous, self._level, self._known = self._level, KILL_LEVEL_HARD, False
        return previous


@dataclass(frozen=True)
class SweepResult:
    listed: int
    cancelled: tuple[str, ...]
    failed: tuple[str, ...]
    unreadable_venues: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.failed and not self.unreadable_venues


class OpenOrderCanceller:
    """Cancel every open order on every venue. Never raises: one bad venue or order must
    not stop the rest. Cancels are sent once each (``Broker.cancel_order`` never retries);
    anything left over is retried by the next sweep."""

    def __init__(self, brokers: Mapping[str, Broker]) -> None:
        self._brokers = dict(brokers)
        self._lock = threading.Lock()  # one sweep at a time

    def sweep(self, reason: str) -> SweepResult:
        with self._lock:
            listed = 0
            cancelled: list[str] = []
            failed: list[str] = []
            unreadable: list[str] = []
            for venue, broker in self._brokers.items():
                count = self._sweep_venue(venue, broker, cancelled, failed)
                if count is None:
                    unreadable.append(venue)
                else:
                    listed += count
        result = SweepResult(listed, tuple(cancelled), tuple(failed), tuple(unreadable))
        log = _log.critical if (listed or not result.complete) else _log.info
        log(
            "kill_switch_cancel_sweep",
            reason=reason,
            listed=listed,
            cancelled=len(cancelled),
            failed=list(failed),
            unreadable_venues=list(unreadable),
        )
        return result

    @staticmethod
    def _sweep_venue(
        venue: str, broker: Broker, cancelled: list[str], failed: list[str]
    ) -> int | None:
        try:
            orders = broker.list_open_orders()
        except Exception as exc:  # noqa: BLE001 - an unreadable venue must not stop the sweep
            _log.critical("kill_sweep_list_failed", venue=venue, error=type(exc).__name__)
            return None
        for order in orders:
            try:
                broker.cancel_order(order.broker_order_id)
            except Exception as exc:  # noqa: BLE001 - keep cancelling the rest
                _log.critical(
                    "kill_sweep_cancel_failed",
                    venue=venue,
                    broker_order_id=order.broker_order_id,
                    error=type(exc).__name__,
                )
                failed.append(order.broker_order_id)
            else:
                cancelled.append(order.broker_order_id)
        return len(orders)


def aegis_state_stream(stub: Any, empty: Callable[[], Any]) -> Callable[[], Iterable[Any]]:
    """``open_stream`` for ``KillSwitchWatcher`` over a generated ``AegisStub``."""
    return lambda: stub.WatchKillSwitchState(empty())


class KillSwitchWatcher:
    """Consumes the Aegis state stream on one thread and re-sweeps on another."""

    def __init__(
        self,
        *,
        open_stream: Callable[[], Iterable[Any]],
        mirror: KillLevelMirror,
        canceller: OpenOrderCanceller,
        resweep_interval_s: float = 5.0,
        reconnect_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        probe: Callable[[], Any] | None = None,
    ) -> None:
        # ``probe`` (e.g. a unary GetKillSwitchState with a short deadline) runs on every
        # sweeper pass while the state is known. The 2026-09-25 dev drill (ALI-170) showed
        # that HTTP/2 keepalive does not break the idle state stream when Aegis is frozen,
        # so without a probe the motor kept a stale NORMAL for 75 s. A failed probe marks
        # the state unknown (HARD) and cancels the stuck stream so it reconnects.
        self._probe = probe
        self._open_stream = open_stream
        self._mirror = mirror
        self._canceller = canceller
        self._resweep_interval_s = resweep_interval_s
        self._backoff_base_s = reconnect_backoff_s
        self._max_backoff_s = max_backoff_s
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._pending_lock = threading.Lock()
        self._pending: str | None = None  # a sweep requested by the stream thread
        self._sweeper_active = False
        self._last_seq: int | None = None
        self._call_lock = threading.Lock()
        self._call: Any = None
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ state handling

    def begin_stream(self) -> None:
        """A new stream's first item is Aegis's current state: accept it whatever its seq."""
        self._last_seq = None

    def handle_state(self, state: Any) -> None:
        seq = int(state.state_seq)
        if self._last_seq is not None and seq < self._last_seq:
            _log.warning("kill_state_out_of_order", seq=seq, last_seq=self._last_seq)
            return
        self._last_seq = seq
        level = int(state.effective_level)
        previous = self._mirror.apply(level)
        current = self._mirror.level()
        if current != previous:
            _log.warning("kill_level_changed", previous=previous, current=current, seq=seq)
        if current >= CANCEL_THRESHOLD:
            self._request_sweep(f"aegis kill level {current} (seq {seq})")

    def handle_stream_down(self, reason: str) -> None:
        """Sweeps on every failed attempt, including the very first one: a motor that
        restarts with orders open while Aegis is unreachable must not wait for the timer."""
        was_known = self._mirror.is_known()
        self._mirror.mark_unknown()
        log = _log.critical if was_known else _log.warning
        log("kill_state_stream_down", reason=reason, treated_as=KILL_LEVEL_HARD)
        self._request_sweep(f"kill state unknown: {reason}")

    def _request_sweep(self, reason: str) -> None:
        """Hand the sweep to the sweeper thread when it runs, so slow broker calls never
        stop the stream thread from reading the next state (e.g. an Aegis reset). Without
        a running sweeper (direct use, tests) sweep inline."""
        if not self._sweeper_active:
            self._canceller.sweep(reason)
            return
        with self._pending_lock:
            self._pending = reason
        self._wake.set()

    def consume(self, stream: Iterable[Any]) -> int:
        """Apply every state from ``stream``; mark unknown when it ends or fails."""
        received = 0
        reason = "stream ended"
        try:
            for state in stream:
                if self._stop.is_set():
                    break
                self.handle_state(state)
                received += 1
        except Exception as exc:  # noqa: BLE001 - any stream failure means state is unknown
            reason = type(exc).__name__
        if self._stop.is_set():
            self._mirror.mark_unknown()  # shutting down: halt, but do not cancel orders
        else:
            self.handle_stream_down(reason)
        return received

    def sweep_if_elevated(self) -> SweepResult | None:
        level = self._mirror.level()
        if level < CANCEL_THRESHOLD:
            return None
        return self._canceller.sweep(f"re-sweep at kill level {level}")

    # ------------------------------------------------------------ threads

    def start(self) -> None:
        if self.is_running():
            raise RuntimeError("kill-switch watcher already running")
        self._stop.clear()
        self._sweeper_active = True
        self._threads = [
            threading.Thread(target=self._stream_loop, name="kill-watch", daemon=True),
            threading.Thread(target=self._sweep_loop, name="kill-sweep", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        with self._call_lock:
            call = self._call
        cancel = getattr(call, "cancel", None)
        if callable(cancel):
            cancel()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout_s)
        self._sweeper_active = False

    def is_running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def _stream_loop(self) -> None:
        backoff = self._backoff_base_s
        while not self._stop.is_set():
            self.begin_stream()
            try:
                call = self._open_stream()
            except Exception as exc:  # noqa: BLE001 - cannot subscribe: state unknown
                self.handle_stream_down(f"subscribe failed: {type(exc).__name__}")
            else:
                with self._call_lock:
                    self._call = call
                    stopping = self._stop.is_set()
                cancel = getattr(call, "cancel", None)
                if stopping and callable(cancel):
                    cancel()  # stop() ran before the call was published
                if self.consume(call):
                    backoff = self._backoff_base_s
                with self._call_lock:
                    self._call = None
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self._max_backoff_s)

    def _sweep_loop(self) -> None:
        while True:
            self._wake.wait(self._resweep_interval_s)
            self._wake.clear()
            if self._stop.is_set():
                return
            with self._pending_lock:
                reason, self._pending = self._pending, None
            if reason is not None:
                self._canceller.sweep(reason)  # requested: sweep even if already reset
            else:
                self.sweep_if_elevated()
            self.check_liveness()

    def check_liveness(self) -> bool:
        """Run the probe while the state is known. On failure treat the state as unknown
        (HARD, sweep) and cancel the current stream so the stream thread reconnects.
        Returns False only when the probe failed."""
        if self._probe is None or not self._mirror.is_known():
            return True
        try:
            self._probe()
        except Exception as exc:  # noqa: BLE001 - any probe failure means state is unknown
            self.handle_stream_down(f"liveness probe failed: {type(exc).__name__}")
            with self._call_lock:
                call = self._call
            cancel = getattr(call, "cancel", None)
            if callable(cancel):
                cancel()
            return False
        return True


def build_kill_guard(
    open_stream: Callable[[], Iterable[Any]],
    brokers: Mapping[str, Broker],
    probe: Callable[[], Any] | None = None,
    **watcher_options: float,
) -> tuple[KillSwitch, KillSwitchWatcher]:
    """The motor's kill switch (mirrors Aegis, no local latch) and the watcher feeding it.
    The switch reads HARD, so halted, until the watcher receives Aegis's first state."""
    mirror = KillLevelMirror()
    kill = KillSwitch(
        start_halted=False,
        signal_source=KillStateHaltSource(mirror.level),
        latch_external=False,
    )
    watcher = KillSwitchWatcher(
        open_stream=open_stream,
        mirror=mirror,
        canceller=OpenOrderCanceller(brokers),
        probe=probe,
        **watcher_options,
    )
    return kill, watcher
