"""Shared test helpers. Identities below are synthetic test labels, not real people."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from afe_sharp import (
    AuditEntry,
    AuditLookup,
    RubricChangeProposal,
    SharpGate,
    Stage,
    StaticRoleAuthorizer,
)
from afe_sharp.ports import AuditSink, ProposalStore

PROPOSER = "reflector-svc"
ALLOWED = {
    Stage.COMPLIANCE: ["compliance-1", "compliance-2"],
    Stage.LEGAL: ["legal-1"],
    Stage.BACKTEST: ["backtest-ci"],
    Stage.RISK: ["risk-1"],
    Stage.CANARY: ["canary-ci", "risk-1"],
    Stage.PROMOTED: ["release-1"],
}
# a valid approver per gate, all distinct
APPROVERS = {
    Stage.COMPLIANCE: "compliance-1",
    Stage.LEGAL: "legal-1",
    Stage.BACKTEST: "backtest-ci",
    Stage.RISK: "risk-1",
    Stage.CANARY: "canary-ci",
    Stage.PROMOTED: "release-1",
}


@dataclass(frozen=True)
class Receipt:
    seq: int
    hash: str


def fake_hash(seq: int, event_type: str, actor: str, payload: Mapping[str, object]) -> str:
    body = json.dumps([seq, event_type, actor, payload], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass
class FakeAudit:
    """AuditSink + AuditLookup. With ``backend``/``lookup`` set (Postgres runs) records go to the
    REAL hash-chained audit table so the insert-time audit-reference trigger can find them."""

    fail: bool = False
    backend: AuditSink | None = None
    lookup: AuditLookup | None = None
    events: list[tuple[str, str, Mapping[str, object]]] = field(default_factory=list)
    entries: dict[int, AuditEntry] = field(default_factory=dict)

    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> Any:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append((event_type, actor, dict(payload)))
        if self.backend is not None:
            return self.backend.record(event_type, actor, payload)
        seq = len(self.entries) + 1
        frozen = json.loads(json.dumps(payload))
        digest = fake_hash(seq, event_type, actor, frozen)
        self.entries[seq] = AuditEntry(seq, digest, event_type, actor, frozen)
        return Receipt(seq, digest)

    def find(self, seqs: Sequence[int]) -> Mapping[int, AuditEntry]:
        if self.lookup is not None:
            return self.lookup.find(seqs)
        return {s: self.entries[s] for s in seqs if s in self.entries}


class Ticker:
    def __init__(self) -> None:
        self._now = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


def make_proposal(pid: str = "p-1", proposer: str = PROPOSER) -> RubricChangeProposal:
    return RubricChangeProposal(
        proposal_id=pid,
        proposer_id=proposer,
        description="raise confidence threshold",
        proposed_change="omega_min: A -> B",
        rationale="underperformance in regime X",
        evidence_refs=("signal:s-1", "report:r-1"),
    )


def make_gate(store: ProposalStore, audit: FakeAudit) -> SharpGate:
    return SharpGate(
        store, audit, StaticRoleAuthorizer(ALLOWED), audit_lookup=audit, clock=Ticker()
    )
