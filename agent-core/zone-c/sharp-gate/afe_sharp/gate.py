"""The enforced SHARP state machine.

Order of effects for every transition (fail closed): validate -> write the audit record -> append to
the store. The audit log and the store are two separate transactions (the audit logger owns its
connection and chain lock), so a transition is NOT atomic across them; consistency comes from the
ordering plus explicit compensation:

* If the audit write fails, nothing is stored (AuditFailureError).
* The stored row carries the audit record's seq/hash; ``get`` (``fold``) rejects any row without a
  matching audit record, so an audit record with no store row simply means "not performed".
* If the store step fails for ANY reason (not only SharpError), the gate checks whether the row
  landed anyway (a lost commit acknowledgement): if so the transition happened and is returned.
  Otherwise it writes an explicit ``sharp.transition_aborted`` audit record (naming the orphaned
  record's seq/hash, the error class and whether the store outcome could be verified) and re-raises
  the original error.
* If that compensating record cannot be written either, ``AbortNotRecordedError`` is raised
  (chained from the original error, carrying the orphaned ``audit_seq``); nothing is swallowed.
  The transition was not performed, but the audit log then holds an unmarked, orphaned record that
  an operator must reconcile.

The reverse order (store first) would allow a promotion with no audit trail, which is the worse
failure.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from afe_sharp.errors import (
    AbortNotRecordedError,
    AuditFailureError,
    DistinctApproverError,
    DuplicateProposalError,
    NotAuthorizedError,
    ProposalClosedError,
    ProposalValidationError,
    SelfApprovalError,
    StageSkipError,
    UnknownProposalError,
)
from afe_sharp.models import (
    ProposalRecord,
    RubricChangeProposal,
    Stage,
    TransitionEvent,
    fold,
    normalise_identity,
    transition_payload,
)
from afe_sharp.ports import ApproverAuthorizer, AuditLookup, AuditReceipt, AuditSink, ProposalStore


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SharpGate:
    def __init__(
        self,
        store: ProposalStore,
        audit: AuditSink,
        authorizer: ApproverAuthorizer,
        *,
        audit_lookup: AuditLookup,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """``audit_lookup`` reads the audit log back: every state read (``get``) verifies that each
        stored transition is backed by a matching audit record, so it is required (fail closed)."""
        self._store = store
        self._audit = audit
        self._audit_lookup = audit_lookup
        self._authorizer = authorizer
        self._clock = clock

    # -- queries -------------------------------------------------------------------------------

    def get(self, proposal_id: str) -> ProposalRecord:
        history = self._store.load(proposal_id)
        if not history:
            raise UnknownProposalError(f"unknown proposal {proposal_id!r}")
        return fold(history, self._audit_lookup)

    # -- commands ------------------------------------------------------------------------------

    def submit(self, proposal: RubricChangeProposal) -> ProposalRecord:
        if self._store.load(proposal.proposal_id):
            raise DuplicateProposalError(f"proposal {proposal.proposal_id!r} already exists")
        event = self._event(proposal.proposal_id, 1, "submitted", None, Stage.DRAFT,
                            proposal.proposer_id, proposal.to_detail())  # fmt: skip
        self._commit(event, expected_version=0)
        return self.get(proposal.proposal_id)

    def approve(
        self,
        proposal_id: str,
        approver_id: str,
        gate: Stage,
        evidence_refs: Sequence[str] = (),
        note: str = "",
    ) -> ProposalRecord:
        record = self._open(proposal_id)
        if gate is not record.next_stage:
            raise StageSkipError(f"next stage is {record.next_stage}, not {gate}")
        who = normalise_identity(approver_id)
        if who == normalise_identity(record.proposal.proposer_id):
            raise SelfApprovalError("the proposer cannot approve their own proposal")
        if any(who == normalise_identity(a.approver_id) for a in record.approvals):
            raise DistinctApproverError("this identity already approved another stage")
        if not self._authorizer.is_authorized(approver_id, gate):
            raise NotAuthorizedError(f"{approver_id!r} may not approve stage {gate}")
        detail = {"evidence_refs": list(evidence_refs), "note": note}
        event = self._event(proposal_id, record.version + 1, "approved", record.state, gate,
                            approver_id, detail)  # fmt: skip
        self._commit(event, expected_version=record.version)
        return self.get(proposal_id)

    def reject(self, proposal_id: str, approver_id: str, reason: str) -> ProposalRecord:
        record = self._open(proposal_id)
        if not reason or not reason.strip():
            raise ProposalValidationError("a rejection reason is required")
        normalise_identity(approver_id)
        next_stage = record.next_stage
        if next_stage is None or not self._authorizer.is_authorized(approver_id, next_stage):
            raise NotAuthorizedError(f"{approver_id!r} may not reject at this stage")
        event = self._event(proposal_id, record.version + 1, "rejected", record.state,
                            Stage.REJECTED, approver_id, {"reason": reason})  # fmt: skip
        self._commit(event, expected_version=record.version)
        return self.get(proposal_id)

    # -- internals -----------------------------------------------------------------------------

    def _open(self, proposal_id: str) -> ProposalRecord:
        record = self.get(proposal_id)
        if record.is_terminal:
            raise ProposalClosedError(f"proposal is {record.state}; no further transitions")
        return record

    def _event(
        self,
        proposal_id: str,
        version: int,
        kind: str,
        from_state: Stage | None,
        to_state: Stage,
        actor: str,
        detail: dict[str, Any],
    ) -> TransitionEvent:
        return TransitionEvent(
            proposal_id=proposal_id,
            version=version,
            kind=kind,
            from_state=from_state,
            to_state=to_state,
            actor=actor,
            occurred_at=self._clock(),
            detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":")),
        )

    def _commit(self, event: TransitionEvent, expected_version: int) -> None:
        payload: dict[str, object] = dict(transition_payload(event))
        try:
            receipt = self._audit.record(f"sharp.{event.kind}", event.actor, payload)
        except Exception as exc:  # noqa: BLE001 - re-raised as AuditFailureError; nothing was stored
            raise AuditFailureError(f"audit refused the transition: {exc}") from exc
        stored = replace(event, **_audit_reference(receipt))
        try:
            self._store.append(stored, expected_version)
        except Exception as exc:  # any failure of step two, not only SharpError; always re-raised
            outcome = self._store_outcome(stored)
            if outcome == "present":
                return  # the row landed and only the acknowledgement was lost: nothing to undo
            self._record_abort(stored, payload, exc, outcome)
            raise

    def _store_outcome(self, stored: TransitionEvent) -> str:
        """Did ``stored`` reach the store despite the error? present | absent | unverified."""
        try:
            history = self._store.load(stored.proposal_id)
        except Exception:  # noqa: BLE001 - we only need to know that we cannot tell
            return "unverified"
        landed = any(
            e.version == stored.version and e.audit_seq == stored.audit_seq
            and e.audit_hash == stored.audit_hash for e in history
        )  # fmt: skip
        return "present" if landed else "absent"

    def _record_abort(
        self, stored: TransitionEvent, payload: dict[str, object], exc: Exception, outcome: str
    ) -> None:
        """Compensating audit record for a transition that was audited but not stored. If it cannot
        be written either, that is raised (AbortNotRecordedError, caused by the original error):
        nothing is swallowed, and the error carries the orphaned record's audit_seq."""
        abort = {**payload, "audit_seq": stored.audit_seq, "audit_hash": stored.audit_hash,
                 "reason": type(exc).__name__, "store_outcome": outcome}  # fmt: skip
        try:
            self._audit.record("sharp.transition_aborted", stored.actor, abort)
        except Exception as abort_exc:  # noqa: BLE001 - re-raised below as AbortNotRecordedError
            raise AbortNotRecordedError(
                f"transition not stored ({type(exc).__name__}: {exc}) and its compensating audit "
                f"record could not be written ({type(abort_exc).__name__}: {abort_exc}); audit "
                f"record {stored.audit_seq} is orphaned",
                audit_seq=stored.audit_seq,
            ) from exc


def _audit_reference(receipt: AuditReceipt) -> dict[str, Any]:
    """The (seq, hash) that ties a stored transition to its audit record. A sink that commits a
    record but cannot say which one is refused: nothing may be stored without the reference."""
    seq, digest = getattr(receipt, "seq", None), getattr(receipt, "hash", None)
    if isinstance(seq, bool) or not isinstance(seq, int) or not isinstance(digest, str):
        raise AuditFailureError("audit sink returned no usable record reference; nothing stored")
    return {"audit_seq": seq, "audit_hash": digest}
