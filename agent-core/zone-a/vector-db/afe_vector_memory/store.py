"""The store contract shared by the Chroma adapter and the in-memory fake."""

from __future__ import annotations

from typing import Protocol

from .models import MemoryHit, MemoryKind, MemoryRecord

DEFAULT_N_RESULTS = 5
DEFAULT_MAX_RESULTS = 20


class MemoryStore(Protocol):
    """Precedent memory. All methods raise `MemoryUnavailableError` if the store fails.

    `query` returns `[]` only when the store answered and nothing matched. Expired
    records are never returned, whether or not `cleanup_expired` has run.
    """

    def add(self, record: MemoryRecord) -> None:
        """Insert or replace the record with the same `record_id`."""
        ...

    def query(
        self,
        text: str,
        *,
        n_results: int = DEFAULT_N_RESULTS,
        regime: str | None = None,
        symbol: str | None = None,
        kind: MemoryKind | None = None,
    ) -> list[MemoryHit]:
        """Nearest records by cosine distance (ascending), optionally filtered."""
        ...

    def cleanup_expired(self) -> int:
        """Delete every expired record and return how many were removed."""
        ...

    def count(self) -> int:
        """Total stored records, including expired ones not yet cleaned up."""
        ...

    def ping(self) -> None:
        """Return normally only if the store is reachable."""
        ...
