"""Async adapter that lets cognitive-core use a `MemoryStore` without importing this package.

cognitive-core defines two structural Protocols (`PrecedentProvider`, `ReflectionWriter`)
in plain types; `AsyncMemoryAdapter` satisfies both. Contract seen by the caller:

* `recall` returns a list. `[]` means the store answered and found nothing.
* Any exception (typically `MemoryUnavailableError`) means "unknown": the caller must not
  treat it as "no precedent".

The stores are synchronous (the Chroma client is), so calls run in a worker thread.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .errors import MemoryUnavailableError
from .models import MemoryHit, MemoryKind, MemoryRecord, Outcome
from .store import MemoryStore

DEFAULT_TOP_K = 3


@dataclass(frozen=True, slots=True)
class PrecedentView:
    """Read-only view of a hit, with plain-typed fields."""

    text: str
    kind: str
    symbol: str
    regime: str
    ts_ms: int
    outcome: str
    distance: float


def _view(hit: MemoryHit) -> PrecedentView:
    record = hit.record
    return PrecedentView(
        text=record.text,
        kind=str(record.kind),
        symbol=record.symbol,
        regime=record.regime,
        ts_ms=record.ts_ms,
        outcome=str(record.outcome),
        distance=hit.distance,
    )


class AsyncMemoryAdapter:
    def __init__(
        self,
        store: MemoryStore,
        *,
        top_k: int = DEFAULT_TOP_K,
        max_distance: float | None = None,
        timeout_s: float | None = None,
    ) -> None:
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if timeout_s is not None and not timeout_s > 0:
            raise ValueError("timeout_s must be positive")
        self._store = store
        self._top_k = top_k
        self._max_distance = max_distance
        self._timeout_s = timeout_s

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking store call off-loop under the per-call deadline (fail closed).

        The worker thread cannot be cancelled: on timeout it is abandoned and the caller
        gets `MemoryUnavailableError`, never an empty result.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs), timeout=self._timeout_s
            )
        except TimeoutError as exc:
            raise MemoryUnavailableError(f"memory call exceeded {self._timeout_s}s") from exc

    async def recall(
        self, *, symbol: str, regime: str, query_text: str, limit: int | None = None
    ) -> list[PrecedentView]:
        """Nearest precedents in the same regime (any symbol), closest first."""
        hits = await self._run(
            self._store.query, query_text, n_results=limit or self._top_k, regime=regime
        )
        return [
            _view(h) for h in hits if self._max_distance is None or h.distance <= self._max_distance
        ]

    async def write_debate(
        self, *, signal_id: str, symbol: str, regime: str, ts_ms: int, outcome: str, text: str
    ) -> None:
        await self._write(
            MemoryKind.DEBATE_OUTCOME, signal_id, symbol, regime, ts_ms, outcome, text
        )

    async def write_reflection(
        self, *, signal_id: str, symbol: str, regime: str, ts_ms: int, outcome: str, text: str
    ) -> None:
        await self._write(
            MemoryKind.POST_TRADE_REFLECTION, signal_id, symbol, regime, ts_ms, outcome, text
        )

    async def _write(
        self,
        kind: MemoryKind,
        signal_id: str,
        symbol: str,
        regime: str,
        ts_ms: int,
        outcome: str,
        text: str,
    ) -> None:
        prefix = "debate" if kind == MemoryKind.DEBATE_OUTCOME else "reflection"
        record = MemoryRecord(
            record_id=f"{prefix}:{signal_id}",
            kind=kind,
            text=text,
            symbol=symbol,
            regime=regime,
            ts_ms=ts_ms,
            outcome=Outcome(outcome),
            signal_id=signal_id,
        )
        await self._run(self._store.add, record)
