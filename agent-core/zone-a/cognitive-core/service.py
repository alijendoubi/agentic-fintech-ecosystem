"""The runner: market snapshot in, debate, proto-mapped TradeSignal out (ALI-40).

Per message (`CognitiveRunner.handle_message`), every step fails closed and none can raise:

1. halted (env / file)              -> nothing happens, no debate, no signal
2. parse + validate the snapshot    -> invalid, stale, warm-up or regime-unknown: dropped
3. symbol allowlist, per-symbol cooldown
4. precedent recall (optional)      -> outage skips the debate when memory is required
5. debate graph under its deadline  -> any failure is already an ABSTAIN signal
6. halted again                     -> a halt raised mid-debate discards the result
7. sink.send (at-most-once)         -> failure is counted, never retried
8. debate outcome written to memory (best effort, after the decision)

Only actionable signals reach the sink unless `emit_abstain` is set. `run` consumes a
`ContextSource` through a bounded drop-oldest queue, so a slow debate can never build up a
backlog of stale snapshots. A source that raises is fatal (`SourceError`); one that simply
ends makes `run` return.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any, Protocol

import structlog

from .config import CognitiveSettings
from .context import ContextError, DebateInput, parse_snapshot
from .graph import run_debate
from .halt import HaltGate
from .health import HealthState
from .memory import (
    PrecedentProvider,
    ReflectionWriter,
    apply_recall,
    recall_precedents,
    write_debate_record,
)
from .models import DebateState, SignalStatus, TradeSignal
from .runner_config import RunnerSettings
from .sinks import SignalSink, SinkError

log = structlog.get_logger(__name__)

QUEUE_SIZE = 64
MAX_TRACKED_SYMBOLS = 10_000
_LOG_ERROR_CHARS = 200


class CycleOutcome(StrEnum):
    HALTED = "halted"
    DROPPED_INVALID = "dropped_invalid"
    SKIPPED_SYMBOL = "skipped_symbol"
    SKIPPED_COOLDOWN = "skipped_cooldown"
    SKIPPED_MEMORY = "skipped_memory_unavailable"
    ABSTAINED = "abstained"
    EMITTED = "emitted"
    SINK_FAILED = "sink_failed"
    ERROR = "error"


class SourceError(RuntimeError):
    """The context source died. The process should exit so the orchestrator restarts it."""


class ContextSource(Protocol):
    """Yields raw snapshot messages (JSON text or bytes); may be infinite."""

    def messages(self) -> AsyncIterator[str | bytes]: ...


class CognitiveRunner:
    def __init__(
        self,
        *,
        settings: RunnerSettings,
        cognitive: CognitiveSettings,
        graph: Any,
        sink: SignalSink,
        halt: HaltGate,
        health: HealthState,
        precedents: PrecedentProvider | None = None,
        reflections: ReflectionWriter | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._cognitive = cognitive
        self._graph = graph
        self._sink = sink
        self._halt = halt
        self._health = health
        self._precedents = precedents
        self._reflections = reflections
        self._clock_ns = clock_ns
        self._monotonic = monotonic
        self._last_debate: dict[str, float] = {}
        self._was_halted = False

    # -- one message -----------------------------------------------------------------

    async def handle_message(self, raw: str | bytes) -> CycleOutcome:
        try:
            outcome = await self._handle(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bug must never turn into a signal
            log.error(
                "cycle_error", error_type=type(exc).__name__, error=str(exc)[:_LOG_ERROR_CHARS]
            )
            outcome = CycleOutcome.ERROR
        self._health.count(outcome.value)
        return outcome

    async def _handle(self, raw: str | bytes) -> CycleOutcome:
        if self._halted():
            return CycleOutcome.HALTED
        try:
            debate_input = parse_snapshot(
                raw, now_ns=self._clock_ns(), max_age_s=self._settings.max_context_age_s
            )
        except ContextError as exc:
            log.info("snapshot_dropped", reason=str(exc)[:_LOG_ERROR_CHARS])
            return CycleOutcome.DROPPED_INVALID
        symbol = debate_input.market_context.symbol
        allowed = self._settings.symbols
        if allowed and symbol not in allowed:
            return CycleOutcome.SKIPPED_SYMBOL
        if not self._cooldown_elapsed(symbol):
            return CycleOutcome.SKIPPED_COOLDOWN
        state = await self._initial_state(debate_input)
        if state is None:
            return CycleOutcome.SKIPPED_MEMORY
        signal = await run_debate(
            self._graph,
            state,
            settings=self._cognitive,
            quantity=self._settings.order_quantity,
        )
        if self._halted():
            log.warning("signal_discarded_halted", signal_id=signal.signal_id, symbol=symbol)
            return CycleOutcome.HALTED
        return await self._deliver(signal)

    # -- steps -----------------------------------------------------------------------

    def _halted(self) -> bool:
        status = self._halt.check()
        self._health.set_halted(status.halted)
        if status.halted != self._was_halted:
            log.warning("halt_state_changed", halted=status.halted, reason=status.reason)
            self._was_halted = status.halted
        return status.halted

    def _cooldown_elapsed(self, symbol: str) -> bool:
        now = self._monotonic()
        last = self._last_debate.get(symbol)
        if last is not None and now - last < self._settings.min_debate_interval_s:
            return False
        if len(self._last_debate) >= MAX_TRACKED_SYMBOLS:
            self._last_debate.clear()
        self._last_debate[symbol] = now  # claimed before the debate: failures still cool down
        return True

    async def _initial_state(self, debate_input: DebateInput) -> DebateState | None:
        state = DebateState(
            market_context=debate_input.market_context,
            regime=debate_input.regime,
            regime_confidence=debate_input.regime_confidence,
        )
        recall = await recall_precedents(
            self._precedents,
            debate_input.market_context,
            debate_input.regime,
            top_k=self._settings.memory_top_k,
            timeout_s=self._settings.memory_timeout_s,
        )
        if recall.status == "unavailable" and self._settings.memory_required:
            log.warning("debate_skipped", reason="memory_unavailable_and_required")
            return None
        return apply_recall(state, recall)

    async def _deliver(self, signal: TradeSignal) -> CycleOutcome:
        actionable = signal.status != SignalStatus.SIGNAL_ABSTAIN
        if actionable or self._settings.emit_abstain:
            try:
                receipt = await self._sink.send(signal)
            except SinkError as exc:
                log.error(
                    "signal_send_failed",
                    signal_id=signal.signal_id,
                    error=str(exc)[:_LOG_ERROR_CHARS],
                )
                return CycleOutcome.SINK_FAILED
            log.info(
                "signal_emitted",
                signal_id=signal.signal_id,
                symbol=signal.symbol,
                status=signal.status.value,
                downstream=receipt.detail,
            )
        await write_debate_record(self._reflections, signal)
        return CycleOutcome.EMITTED if actionable else CycleOutcome.ABSTAINED

    # -- the loop --------------------------------------------------------------------

    async def run(self, source: ContextSource, stop: asyncio.Event) -> None:
        """Consume `source` until `stop` is set. Raises `SourceError` if the source dies."""
        queue: asyncio.Queue[str | bytes] = asyncio.Queue(maxsize=QUEUE_SIZE)
        pump = asyncio.create_task(self._pump(source, queue), name="context-pump")
        try:
            while not stop.is_set():
                self._health.beat()
                if pump.done() and queue.empty():
                    self._finish(pump)
                    return
                try:
                    raw = await asyncio.wait_for(
                        queue.get(), timeout=self._settings.heartbeat_interval_s
                    )
                except TimeoutError:
                    continue
                await self.handle_message(raw)
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            self._health.stop()

    async def _pump(self, source: ContextSource, queue: asyncio.Queue[str | bytes]) -> None:
        async for raw in source.messages():
            if queue.full():
                queue.get_nowait()  # drop the oldest: fresh data beats a stale backlog
                self._health.count("queue_dropped")
            queue.put_nowait(raw)

    @staticmethod
    def _finish(pump: asyncio.Task[None]) -> None:
        """The pump ended: a clean end returns (finite source); an error is fatal."""
        exc = None if pump.cancelled() else pump.exception()
        if exc is not None:
            raise SourceError(f"context source failed: {type(exc).__name__}: {exc}") from exc
