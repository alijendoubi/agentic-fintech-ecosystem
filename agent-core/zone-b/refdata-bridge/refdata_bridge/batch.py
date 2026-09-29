"""Accumulates the latest known snapshot and the latest regime label per symbol,
and builds the next batch to push to Aegis.

Only items newer than what this bridge last successfully pushed are re-sent:
Aegis rejects a same-or-older ``as_of_ns`` / ``timestamp_ns`` as ``out_of_order``
(see ``agent-core/zone-b/aegis/README.md`` "Reference data feed"), so re-pushing
an unchanged value every cycle would just generate noisy rejections.

Regime labels are tracked PER SYMBOL (ALI-158): ``RegimeLabelPacket`` carries a
``symbol`` and Aegis stores and judges C18 per symbol, so a newer label for one
symbol never replaces another symbol's label. (Before ALI-158 the proto had no
symbol and this bridge could only forward the single newest label across all
symbols.)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from refdata_bridge.mapping import ReferenceSnapshotData, RegimeLabelData


@dataclass(frozen=True, slots=True)
class PendingBatch:
    snapshots: list[ReferenceSnapshotData]
    regimes: list[RegimeLabelData]

    @property
    def is_empty(self) -> bool:
        return not self.snapshots and not self.regimes


class BatchState:
    """Thread-safe (asyncio-safe: no ``await`` while holding the lock) accumulator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, ReferenceSnapshotData] = {}
        self._last_pushed_as_of: dict[str, int] = {}
        self._latest_regimes: dict[str, RegimeLabelData] = {}
        self._last_pushed_regime_ts: dict[str, int] = {}

    def record_snapshot(self, data: ReferenceSnapshotData) -> None:
        with self._lock:
            current = self._latest.get(data.symbol)
            if current is not None and data.as_of_ns < current.as_of_ns:
                return  # out-of-order: keep the newer one we already have
            self._latest[data.symbol] = data

    def record_regime(self, data: RegimeLabelData) -> None:
        with self._lock:
            current = self._latest_regimes.get(data.symbol)
            if current is not None and data.timestamp_ns < current.timestamp_ns:
                return  # out-of-order for this symbol: keep the newer one
            self._latest_regimes[data.symbol] = data

    def build_batch(self, max_snapshots: int, max_regimes: int | None = None) -> PendingBatch:
        """Items not yet pushed (or newer than the last push), oldest first."""
        limit = max_snapshots if max_regimes is None else max_regimes
        with self._lock:
            due = [
                snap
                for symbol, snap in self._latest.items()
                if snap.as_of_ns > self._last_pushed_as_of.get(symbol, 0)
            ]
            due.sort(key=lambda s: s.as_of_ns)
            regimes = [
                r
                for symbol, r in self._latest_regimes.items()
                if r.timestamp_ns > self._last_pushed_regime_ts.get(symbol, 0)
            ]
            regimes.sort(key=lambda r: r.timestamp_ns)
            return PendingBatch(snapshots=due[:max_snapshots], regimes=regimes[:limit])

    def mark_applied(
        self, applied_symbols: list[str], applied_regimes: list[RegimeLabelData]
    ) -> None:
        """Advance the per-item watermarks for items Aegis actually accepted."""
        with self._lock:
            for symbol in applied_symbols:
                snap = self._latest.get(symbol)
                if snap is not None:
                    self._last_pushed_as_of[symbol] = snap.as_of_ns
            for regime in applied_regimes:
                previous = self._last_pushed_regime_ts.get(regime.symbol, 0)
                self._last_pushed_regime_ts[regime.symbol] = max(previous, regime.timestamp_ns)

    def known_symbols(self) -> int:
        with self._lock:
            return len(self._latest)
