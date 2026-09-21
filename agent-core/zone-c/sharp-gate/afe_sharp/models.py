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
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

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
    # Reference to the hash-chained audit record written for this transition (set by the gate
    # after the audit write; a stored row without it is rejected by the database and by fold()).
    audit_seq: int | None = None
    audit_hash: str | None = None

    @property
    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = json.loads(self.detail_json)
        return detail


@dataclass(frozen=True)
class AuditEntry:
    """A record read back from the hash-chained audit log."""

    seq: int
    hash: str
    event_type: str
    actor: str
    payload: Mapping[str, Any]


class AuditLookup(Protocol):
    """Read side of the audit log. Returns the records that exist for the given sequence numbers
    (absent ones are simply missing from the result); raises StoreError if it cannot answer."""

    def find(self, seqs: Sequence[int]) -> Mapping[int, AuditEntry]: ...


def transition_payload(event: TransitionEvent) -> dict[str, Any]:
    """The payload written to the audit log for a transition. The database trigger rebuilds the
    same document from the row, so the two must stay identical."""
    return {
        "proposal_id": event.proposal_id,
        "version": event.version,
        "kind": event.kind,
        "from_state": event.from_state.value if event.from_state else None,
        "to_state": event.to_state.value,
        "occurred_at": event.occurred_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "detail": event.detail,
    }


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


def _stored_detail(event: TransitionEvent) -> dict[str, Any]:
    """The event's detail as an object; anything else means the stored row is corrupt."""
    try:
        detail = json.loads(event.detail_json)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"stored detail is not valid JSON at version {event.version}") from exc
    if not isinstance(detail, dict):
        raise StoreError(f"stored detail is not an object at version {event.version}")
    return detail


def _stored_identity(event: TransitionEvent) -> str:
    try:
        return normalise_identity(event.actor)
    except ProposalValidationError as exc:
        raise StoreError(f"invalid actor at version {event.version}: {exc}") from exc


def _stored_proposal(first: TransitionEvent) -> RubricChangeProposal:
    detail = _stored_detail(first)
    try:
        refs = tuple(detail["evidence_refs"])
        proposal = RubricChangeProposal(**{**detail, "evidence_refs": refs})
    except (TypeError, KeyError, ProposalValidationError) as exc:
        raise StoreError(f"stored proposal is invalid: {exc}") from exc
    if first.actor != proposal.proposer_id:
        raise StoreError("submitted event actor is not the proposer named in the proposal")
    return proposal


def _verify_audit_records(events: Sequence[TransitionEvent], audit: AuditLookup) -> None:
    """Every stored transition must reference an existing audit-chain record that says the same
    thing (hash, event type, actor and the full transition payload); one record backs one row."""
    seqs: list[int] = []
    for event in events:
        seq, digest = event.audit_seq, event.audit_hash
        if isinstance(seq, bool) or not isinstance(seq, int) or not isinstance(digest, str):
            raise StoreError(f"transition {event.version} has no audit reference")
        seqs.append(seq)
    if len(set(seqs)) != len(seqs):
        raise StoreError("one audit record is referenced by more than one transition")
    found = audit.find(seqs)
    for event, seq in zip(events, seqs, strict=True):
        entry = found.get(seq)
        if entry is None:
            raise StoreError(f"no audit record {seq} for transition {event.version}")
        if (
            entry.hash != event.audit_hash
            or entry.event_type != f"sharp.{event.kind}"
            or entry.actor != event.actor
            or dict(entry.payload) != transition_payload(event)
        ):
            raise StoreError(f"audit record {seq} does not match transition {event.version}")


def fold(events: Sequence[TransitionEvent], audit: AuditLookup) -> ProposalRecord:
    """Rebuild the current record from history, re-verifying every invariant the database trigger
    enforces at insert time (sequential versions, legal transitions, no stage skips, submitter
    never approves, one stage per approver, a rejection needs a reason) AND that each transition is
    backed by a matching audit-chain record. Any inconsistency raises StoreError: a history that is
    not fully valid is never reported as advanced or PROMOTED."""
    if not events or events[0].kind != "submitted":
        raise StoreError("history must start with a 'submitted' event")
    proposal = _stored_proposal(events[0])
    submitter = normalise_identity(proposal.proposer_id)
    state, approvals, signers = Stage.DRAFT, [], set[str]()
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
            who = _stored_identity(event)
            if who == submitter:
                raise StoreError(f"stored self-approval at version {index}")
            if who in signers:
                raise StoreError(f"stored repeated approver at version {index}")
            signers.add(who)
            approvals.append(_approval(event))
        elif event.kind == "rejected" and event.to_state is Stage.REJECTED:
            _stored_identity(event)
            reason = _stored_detail(event).get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise StoreError(f"stored rejection without a reason at version {index}")
            rejected_by = event.actor
        else:
            raise StoreError(f"unknown event kind at version {index}")
        state = event.to_state
    _verify_audit_records(events, audit)
    return ProposalRecord(proposal, state, len(events), tuple(approvals), rejected_by, reason)


def _approval(event: TransitionEvent) -> Approval:
    detail = _stored_detail(event)
    return Approval(
        stage=event.to_state,
        approver_id=event.actor,
        occurred_at=event.occurred_at,
        evidence_refs=tuple(detail.get("evidence_refs", ())),
        note=str(detail.get("note", "")),
    )
