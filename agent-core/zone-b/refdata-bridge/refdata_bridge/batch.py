"""Accumulates the latest known snapshot per symbol and the latest regime label,
and builds the next batch to push to Aegis.

Only items newer than what this bridge last successfully pushed are re-sent:
Aegis rejects a same-or-older ``as_of_ns`` / ``timestamp_ns`` as ``out_of_order``
(see ``agent-core/zone-b/aegis/README.md`` "Reference data feed"), so re-pushing
an unchanged value every cycle would just generate noisy rejections.

Known assumption (see README "Known limitations"): ``RegimeLabelPacket``
(market_snapshot.proto) carries no symbol, so Aegis's reference-data store holds
exactly one global regime label, not one per symbol. This bridge therefore keeps
only the single most-recently-timestamped regime message across ALL symbols that
regime-detector publishes on ``regime:labels`` and forwards that one. If Zone A
ever needs a per-symbol regime signal at Aegis, the proto contract must add a
symbol field first.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from refdata_bridge.mapping import ReferenceSnapshotData, RegimeLabelData


@dataclass(frozen=True, slots=True)
class PendingBatch:
    snapshots: list[ReferenceSnapshotData]
    regime: RegimeLabelData | None


class BatchState:
    """Thread-safe (asyncio-safe: no ``await`` while holding the lock) accumulator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, ReferenceSnapshotData] = {}
        self._last_pushed_as_of: dict[str, int] = {}
        self._latest_regime: RegimeLabelData | None = None
        self._last_pushed_regime_ts: int | None = None

    def record_snapshot(self, data: ReferenceSnapshotData) -> None:
        with self._lock:
            current = self._latest.get(data.symbol)
            if current is not None and data.as_of_ns < current.as_of_ns:
                return  # out-of-order: keep the newer one we already have
            self._latest[data.symbol] = data

    def record_regime(self, data: RegimeLabelData) -> None:
        with self._lock:
            current = self._latest_regime
            if current is not None and data.timestamp_ns < current.timestamp_ns:
                return  # out-of-order across symbols: keep the newer one
            self._latest_regime = data

    def build_batch(self, max_snapshots: int) -> PendingBatch:
        """Snapshots/regime not yet pushed (or newer than last push), oldest-symbol-first."""
        with self._lock:
            due = [
                snap
                for symbol, snap in self._latest.items()
                if snap.as_of_ns > self._last_pushed_as_of.get(symbol, 0)
            ]
            due.sort(key=lambda s: s.as_of_ns)
            snapshots = due[:max_snapshots]
            regime = self._latest_regime
            if (
                regime is not None
                and self._last_pushed_regime_ts is not None
                and regime.timestamp_ns <= self._last_pushed_regime_ts
            ):
                regime = None
            return PendingBatch(snapshots=snapshots, regime=regime)

    def mark_applied(
        self, applied_symbols: list[str], regime_applied: bool, regime_ts: int | None
    ) -> None:
        """Advance the per-item watermarks for items Aegis actually accepted."""
        with self._lock:
            for symbol in applied_symbols:
                snap = self._latest.get(symbol)
                if snap is not None:
                    self._last_pushed_as_of[symbol] = snap.as_of_ns
            if regime_applied and regime_ts is not None:
                self._last_pushed_regime_ts = regime_ts

    def known_symbols(self) -> int:
        with self._lock:
            return len(self._latest)
