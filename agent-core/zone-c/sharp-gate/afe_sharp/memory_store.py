"""In-memory ProposalStore for tests and single-process use (not durable)."""

from __future__ import annotations

import threading

from afe_sharp.errors import ConcurrencyError
from afe_sharp.models import TransitionEvent


class InMemoryProposalStore:
    def __init__(self) -> None:
        self._events: dict[str, list[TransitionEvent]] = {}
        self._lock = threading.Lock()

    def load(self, proposal_id: str) -> list[TransitionEvent]:
        with self._lock:
            return list(self._events.get(proposal_id, ()))

    def append(self, event: TransitionEvent, expected_version: int) -> None:
        with self._lock:
            history = self._events.setdefault(event.proposal_id, [])
            if len(history) != expected_version or event.version != expected_version + 1:
                raise ConcurrencyError(
                    f"expected version {expected_version}, store has {len(history)}"
                )
            history.append(event)
