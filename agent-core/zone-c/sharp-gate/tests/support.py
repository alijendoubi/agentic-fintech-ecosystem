"""Shared test helpers. Identities below are synthetic test labels, not real people."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from afe_sharp import RubricChangeProposal, SharpGate, Stage, StaticRoleAuthorizer
from afe_sharp.ports import ProposalStore

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


@dataclass
class FakeAudit:
    fail: bool = False
    events: list[tuple[str, str, Mapping[str, object]]] = field(default_factory=list)

    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append((event_type, actor, dict(payload)))


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


def make_gate(store: ProposalStore, audit: FakeAudit | object) -> SharpGate:
    return SharpGate(store, audit, StaticRoleAuthorizer(ALLOWED), clock=Ticker())  # type: ignore[arg-type]
