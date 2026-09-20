"""Domain model of the SHARP promotion gate (docs/processes/sharp-promotion.md).

State names the LAST GATE PASSED: DRAFT (proposal recorded, nothing signed) -> COMPLIANCE (step 2
signed) -> LEGAL
(step 3) -> BACKTEST (step 4, automated backtesting result signed by its CI identity) -> RISK (step
5) -> CANARY
(step 6) -> PROMOTED (step 7, full promotion). REJECTED is reachable from any non-terminal state.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from afe_sharp.errors import ProposalValidationError, StoreError


class Stage(StrEnum):
    DRAFT = "DRAFT"
    COMPLIANCE = "COMPLIANCE"
    LEGAL = "LEGAL"
    BACKTEST = "BACKTEST"
    RISK = "RISK"
    CANARY = "CANARY"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"


PIPELINE: tuple[Stage, ...] = (
    Stage.DRAFT,
    Stage.COMPLIANCE,
    Stage.LEGAL,
    Stage.BACKTEST,
    Stage.RISK,
    Stage.CANARY,
    Stage.PROMOTED,
)
TERMINAL = frozenset({Stage.PROMOTED, Stage.REJECTED})
MAX_TEXT = 20_000


def normalise_identity(identity: str) -> str:
    """Comparison form of an identity (NFKC, trimmed, case-folded) so 'Alice ' and 'alice' are one
    person."""
    if not isinstance(identity, str) or not identity.strip():
        raise ProposalValidationError("identity must be a non-empty string")
    if "\x00" in identity:
        raise ProposalValidationError("identity contains NUL")
    return unicodedata.normalize("NFKC", identity).strip().casefold()


def _text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProposalValidationError(f"{name} must be a non-empty string")
    if len(value) > MAX_TEXT or "\x00" in value:
        raise ProposalValidationError(f"{name} is too long or contains NUL")
    return value


@dataclass(frozen=True)
class RubricChangeProposal:
    """Schema the Zone A Reflector's RubricChangeProposal must supply (see ``from_reflector``)."""

    proposal_id: str
    proposer_id: str
    description: str
    proposed_change: str
    rationale: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _text("proposal_id", self.proposal_id)
        normalise_identity(self.proposer_id)
        _text("description", self.description)
        _text("proposed_change", self.proposed_change)
        _text("rationale", self.rationale)
        if not self.evidence_refs:
            raise ProposalValidationError("at least one evidence reference is required")
        for ref in self.evidence_refs:
            _text("evidence_refs[]", ref)

    @classmethod
    def from_reflector(cls, data: Mapping[str, Any], proposer_id: str) -> RubricChangeProposal:
        """Map the current Zone A ``RubricChangeProposal`` fields (proposal_id, trigger_signal_id,
        observed_underperformance, proposed_change, rationale) or the native field names.
        ``proposer_id`` is
        the authenticated identity of the submitting service, supplied by the caller (never invented
        here)."""
        refs: list[str] = list(data.get("evidence_refs", ()))
        if data.get("trigger_signal_id"):
            refs.append(f"signal:{data['trigger_signal_id']}")
        try:
            return cls(
                proposal_id=data["proposal_id"],
                proposer_id=proposer_id,
                description=data.get("description") or data["observed_underperformance"],
                proposed_change=data["proposed_change"],
                rationale=data["rationale"],
                evidence_refs=tuple(refs),
            )
        except KeyError as exc:
            raise ProposalValidationError(f"missing proposal field: {exc.args[0]}") from exc

    def to_detail(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposer_id": self.proposer_id,
            "description": self.description,
            "proposed_change": self.proposed_change,
            "rationale": self.rationale,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class TransitionEvent:
    """One immutable, append-only history entry. kind: submitted | approved | rejected."""

    proposal_id: str
    version: int
    kind: str
    from_state: Stage | None
    to_state: Stage
    actor: str
    occurred_at: datetime
    detail_json: str = field(default="{}")

    @property
    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = json.loads(self.detail_json)
        return detail


@dataclass(frozen=True)
class Approval:
    stage: Stage
    approver_id: str
    occurred_at: datetime
    evidence_refs: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class ProposalRecord:
    proposal: RubricChangeProposal
    state: Stage
    version: int
    approvals: tuple[Approval, ...]
    rejected_by: str | None = None
    rejection_reason: str | None = None

    @property
    def next_stage(self) -> Stage | None:
        if self.state in TERMINAL:
            return None
        return PIPELINE[PIPELINE.index(self.state) + 1]

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL


def fold(events: Sequence[TransitionEvent]) -> ProposalRecord:
    """Rebuild the current record from history, validating every step. Inconsistent history =>
    StoreError."""
    if not events or events[0].kind != "submitted":
        raise StoreError("history must start with a 'submitted' event")
    first = events[0]
    try:
        proposal = RubricChangeProposal(
            **{**first.detail, "evidence_refs": tuple(first.detail["evidence_refs"])}
        )
    except (TypeError, KeyError, ProposalValidationError) as exc:
        raise StoreError(f"stored proposal is invalid: {exc}") from exc
    state, approvals = Stage.DRAFT, []
    rejected_by = reason = None
    for index, event in enumerate(events, start=1):
        if event.version != index or event.proposal_id != proposal.proposal_id:
            raise StoreError(f"history out of sequence at version {event.version}")
        if index == 1:
            if event.to_state is not Stage.DRAFT or event.from_state is not None:
                raise StoreError("submitted event must create DRAFT")
            continue
        if state in TERMINAL or event.from_state is not state:
            raise StoreError(f"illegal stored transition at version {index}")
        if event.kind == "approved":
            if PIPELINE[PIPELINE.index(state) + 1] is not event.to_state:
                raise StoreError(f"stored stage skip at version {index}")
            approvals.append(_approval(event))
        elif event.kind == "rejected" and event.to_state is Stage.REJECTED:
            rejected_by, reason = event.actor, str(event.detail.get("reason", ""))
        else:
            raise StoreError(f"unknown event kind at version {index}")
        state = event.to_state
    return ProposalRecord(proposal, state, len(events), tuple(approvals), rejected_by, reason)


def _approval(event: TransitionEvent) -> Approval:
    detail = event.detail
    return Approval(
        stage=event.to_state,
        approver_id=event.actor,
        occurred_at=event.occurred_at,
        evidence_refs=tuple(detail.get("evidence_refs", ())),
        note=str(detail.get("note", "")),
    )
