"""Interfaces the gate depends on (injected; no identity or credential is hard-coded anywhere)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from afe_sharp.models import AuditEntry, AuditLookup, Stage, TransitionEvent, normalise_identity

__all__ = [
    "ApproverAuthorizer",
    "AuditEntry",
    "AuditLookup",
    "AuditReceipt",
    "AuditSink",
    "ProposalStore",
    "StaticRoleAuthorizer",
]


class AuditReceipt(Protocol):
    """What a committed audit write returns: the record's position and hash in the chain (both
    are stored with the transition and verified on every read)."""

    @property
    def seq(self) -> int: ...

    @property
    def hash(self) -> str: ...


class AuditSink(Protocol):
    """Structurally satisfied by ``afe_audit.AuditLogger``. Raising means the transition must not
    happen. The returned receipt must identify the committed record."""

    def record(
        self, event_type: str, actor: str, payload: Mapping[str, object]
    ) -> AuditReceipt: ...


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
