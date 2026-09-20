"""Inference / retraining loop.

Fail-closed contract: for every known symbol, every cycle publishes either a
confident regime or ``REGIME_UNKNOWN`` with a reason. UNKNOWN is published when
the model is untrained, data is missing, stale or too short, QuestDB is down,
confidence is below the floor, or anything unexpected happens.

Training (``GaussianHMM.fit``) is CPU-bound, so it runs in an executor thread
as a background task; the loop keeps publishing meanwhile. A failed training
attempt never resets the retrain clock: it only schedules a retry after
``train_retry_backoff_s`` (ALI-29).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import structlog
from regime_detector.config import Settings
from regime_detector.features import extract_features
from regime_detector.labels import RegimeLabel
from regime_detector.model import (
    ModelError,
    Prediction,
    TrainedModel,
    apply_confidence_floor,
    predict_regime,
    train_model,
    unknown,
)
from regime_detector.model_store import ModelStore
from regime_detector.publisher import RegimeMessage, RegimePublisher
from regime_detector.questdb_client import BarRows, QuestDbClient

log = structlog.get_logger()

Trainer = Callable[[np.ndarray, float], TrainedModel]
Clock = Callable[[], float]
_NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class SymbolState:
    """Immutable per-symbol model state; replaced (never mutated) on change."""

    model: TrainedModel | None = None
    next_train_at: float = 0.0


class RegimeService:
    """Owns per-symbol models and produces one message per symbol per cycle."""

    def __init__(
        self,
        settings: Settings,
        questdb: QuestDbClient,
        publisher: RegimePublisher,
        store: ModelStore,
        *,
        clock: Clock = time.time,
        trainer: Trainer = train_model,
        executor: Executor | None = None,
    ) -> None:
        self._settings = settings
        self._questdb = questdb
        self._publisher = publisher
        self._store = store
        self._clock = clock
        self._trainer = trainer
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="hmm")
        self._states: dict[str, SymbolState] = {}
        self._training: dict[str, asyncio.Task[TrainedModel | None]] = {}
        self._known_symbols: list[str] = []

    # ── public API ──────────────────────────────────────────────────────────

    async def run_cycle(self) -> list[RegimeMessage]:
        """One inference pass over all symbols; returns what was attempted to publish."""
        self._collect_training()
        symbols = await self._questdb.list_symbols()
        if symbols is None:
            outage = unknown("questdb_unavailable")
            messages = [self._message(s, outage) for s in self._known_symbols]
        else:
            self._known_symbols = symbols
            messages = [await self._evaluate_safe(s) for s in symbols]
        for message in messages:
            await self._publisher.publish(message)
        return messages

    async def run(self, stop: asyncio.Event) -> None:
        """Loop until ``stop`` is set."""
        while not stop.is_set():
            started = time.monotonic()
            await self._guarded_cycle()
            self._touch_heartbeat()
            delay = max(0.0, self._settings.inference_interval_s - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue
        await self.shutdown()

    async def drain_training(self) -> None:
        """Wait for in-flight training tasks (used by tests and shutdown paths)."""
        if self._training:
            await asyncio.gather(*self._training.values(), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in self._training.values():
            task.cancel()
        self._training.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        await self._questdb.close()

    # ── cycle internals ─────────────────────────────────────────────────────

    async def _guarded_cycle(self) -> None:
        try:
            await self.run_cycle()
        except Exception:  # noqa: BLE001 - the loop must survive; abstain instead
            log.exception("regime_cycle_failed")
            for symbol in self._known_symbols:
                await self._publisher.publish(self._message(symbol, unknown("internal_error")))

    async def _evaluate_safe(self, symbol: str) -> RegimeMessage:
        try:
            return self._message(symbol, await self._evaluate(symbol))
        except Exception:  # noqa: BLE001 - one symbol must not take down the others
            log.exception("regime_symbol_failed", symbol=symbol)
            return self._message(symbol, unknown("internal_error"))

    async def _evaluate(self, symbol: str) -> Prediction:
        rows = await self._questdb.fetch_rows(symbol)
        if rows is None:
            return unknown("no_data")
        stale = self._staleness_reason(rows)
        if stale is not None:
            return unknown(stale)
        build = extract_features(rows.mid_prices, rows.spreads, rows.ofis)
        if build.matrix is None or build.rows_used < self._settings.min_bars_for_inference:
            reason = build.reason or "insufficient_rows"
            log.info("regime_skip", symbol=symbol, reason=reason, rows_used=build.rows_used)
            return unknown(f"insufficient_data:{reason}")
        state = self._state_for(symbol)
        self._maybe_start_training(symbol, state, build.matrix)
        prediction = predict_regime(state.model, build.matrix)
        return apply_confidence_floor(prediction, self._settings.min_confidence)

    def _staleness_reason(self, rows: BarRows) -> str | None:
        if rows.latest_ts_ns is None:
            return "bad_timestamp"
        if rows.latest_is_stale:
            return "source_flagged_stale"
        age_s = self._clock() - rows.latest_ts_ns / _NS
        limit = self._settings.max_data_age_s
        if age_s > limit or age_s < -limit:
            return "stale_data"
        return None

    def _message(self, symbol: str, prediction: Prediction) -> RegimeMessage:
        return RegimeMessage(
            symbol=symbol,
            label=prediction.label,
            confidence=0.0 if prediction.label is RegimeLabel.UNKNOWN else prediction.confidence,
            ts_ns=int(self._clock() * _NS),
            reason=prediction.reason,
        )

    # ── model lifecycle ─────────────────────────────────────────────────────

    def _state_for(self, symbol: str) -> SymbolState:
        """Existing state, or a verified model loaded from disk on first sight."""
        state = self._states.get(symbol)
        if state is not None:
            return state
        model = self._store.load(symbol)
        next_train = (model.trained_at + self._settings.retrain_interval_s) if model else 0.0
        state = SymbolState(model=model, next_train_at=next_train)
        self._states[symbol] = state
        return state

    def _maybe_start_training(self, symbol: str, state: SymbolState, matrix: np.ndarray) -> None:
        if symbol in self._training or self._clock() < state.next_train_at:
            return
        if len(matrix) < self._settings.min_train_rows:
            return
        snapshot = matrix.copy()  # the worker thread gets its own copy
        self._training[symbol] = asyncio.get_running_loop().create_task(
            self._train(symbol, snapshot)
        )

    async def _train(self, symbol: str, matrix: np.ndarray) -> TrainedModel | None:
        loop = asyncio.get_running_loop()
        try:
            model = await loop.run_in_executor(self._executor, self._trainer, matrix, self._clock())
        except ModelError as exc:
            log.error("hmm_training_failed", symbol=symbol, error=str(exc))
            return None
        await loop.run_in_executor(self._executor, self._store.save, symbol, model)
        log.info("hmm_trained", symbol=symbol, rows=len(matrix))
        return model

    def _collect_training(self) -> None:
        """Install finished models; on failure keep the old model and back off."""
        for symbol, task in list(self._training.items()):
            if not task.done():
                continue
            del self._training[symbol]
            model = self._finished_model(symbol, task)
            self._states[symbol] = self._after_training(self._states.get(symbol), model)

    @staticmethod
    def _finished_model(
        symbol: str, task: asyncio.Task[TrainedModel | None]
    ) -> TrainedModel | None:
        if task.cancelled():
            return None
        error = task.exception()
        if error is not None:
            log.error("hmm_training_crashed", symbol=symbol, error=str(error))
            return None
        return task.result()

    def _after_training(self, state: SymbolState | None, model: TrainedModel | None) -> SymbolState:
        current = state or SymbolState()
        if model is not None:  # only a success moves the retrain clock
            return SymbolState(model, model.trained_at + self._settings.retrain_interval_s)
        return replace(current, next_train_at=self._clock() + self._settings.train_retry_backoff_s)

    def _touch_heartbeat(self) -> None:
        path: Path = self._settings.heartbeat_path
        try:
            path.touch()
        except OSError as exc:
            log.warning("heartbeat_failed", path=str(path), error=str(exc))
