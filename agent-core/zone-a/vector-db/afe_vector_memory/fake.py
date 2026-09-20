"""In-memory `MemoryStore` for tests: same contract, no server, no network."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable

from .embedding import Embedder, HashingEmbedder, embed_checked
from .errors import MemoryUnavailableError, MemoryValidationError
from .models import MemoryHit, MemoryKind, MemoryRecord, RetentionPolicy, validate_query_filters
from .store import DEFAULT_MAX_RESULTS, DEFAULT_N_RESULTS


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / norm


class InMemoryStore:
    """Thread-safe fake. `fail_with` makes every call raise, to test fail-closed callers."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        retention: RetentionPolicy | None = None,
        clock: Callable[[], int] = wall_clock_ms,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> None:
        self._embedder = embedder or HashingEmbedder()
        self._retention = retention or RetentionPolicy()
        self._clock = clock
        self._max_results = max_results
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[MemoryRecord, list[float], int]] = {}
        self.fail_with: Exception | None = None

    def _guard(self) -> None:
        if self.fail_with is not None:
            raise MemoryUnavailableError(f"fake store forced failure: {self.fail_with}")

    def add(self, record: MemoryRecord) -> None:
        self._guard()
        if not isinstance(record, MemoryRecord):
            raise MemoryValidationError("add() requires a MemoryRecord")
        vector = embed_checked(self._embedder, [record.text])[0]
        expires = self._retention.expires_at_ms(record.kind, record.ts_ms)
        with self._lock:
            self._rows[record.record_id] = (record, vector, expires)

    def query(
        self,
        text: str,
        *,
        n_results: int = DEFAULT_N_RESULTS,
        regime: str | None = None,
        symbol: str | None = None,
        kind: MemoryKind | None = None,
    ) -> list[MemoryHit]:
        self._guard()
        validate_query_filters(
            n_results=n_results,
            regime=regime,
            symbol=symbol,
            kind=kind,
            max_results=self._max_results,
        )
        if not isinstance(text, str) or not text.strip():
            raise MemoryValidationError("query text must be a non-empty string")
        query_vector = embed_checked(self._embedder, [text])[0]
        now = self._clock()
        with self._lock:
            rows = list(self._rows.values())
        hits = [
            MemoryHit(record, _cosine_distance(query_vector, vector))
            for record, vector, expires in rows
            if expires > now
            and (regime is None or record.regime == regime)
            and (symbol is None or record.symbol == symbol)
            and (kind is None or record.kind == kind)
        ]
        hits.sort(key=lambda hit: (hit.distance, hit.record.record_id))
        return hits[:n_results]

    def cleanup_expired(self) -> int:
        self._guard()
        now = self._clock()
        with self._lock:
            expired = [rid for rid, (_, _, expires) in self._rows.items() if expires <= now]
            for rid in expired:
                del self._rows[rid]
        return len(expired)

    def count(self) -> int:
        self._guard()
        with self._lock:
            return len(self._rows)

    def ping(self) -> None:
        self._guard()
