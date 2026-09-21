"""Interfaces the gate depends on (injected; no identity or credential is hard-coded anywhere)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from afe_sharp.models import Stage, TransitionEvent, normalise_identity


class AuditSink(Protocol):
    """Structurally satisfied by ``afe_audit.AuditLogger``. Raising means the transition must not
    happen."""

    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> Any: ...


class ProposalStore(Protocol):
    """Append-only history. ``append`` must fail with ConcurrencyError unless exactly
    ``expected_version``
    events already exist for the proposal; stored events are never modified or removed."""

    def load(self, proposal_id: str) -> list[TransitionEvent]: ...

    def append(self, event: TransitionEvent, expected_version: int) -> None: ...


class ApproverAuthorizer(Protocol):
    def is_authorized(self, identity: str, stage: Stage) -> bool: ...


class StaticRoleAuthorizer:
    """Authorizer over a caller-supplied mapping ``stage -> identities`` (loaded from the owner's
    configuration).
    Unlisted stages deny everyone (fail closed)."""

    def __init__(self, allowed: Mapping[Stage, Iterable[str]]) -> None:
        self._allowed = {
            stage: frozenset(normalise_identity(i) for i in ids) for stage, ids in allowed.items()
        }

    def is_authorized(self, identity: str, stage: Stage) -> bool:
        return normalise_identity(identity) in self._allowed.get(stage, frozenset())
