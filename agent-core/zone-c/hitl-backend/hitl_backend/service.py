"""Hold listing and operator decisions (contract sections 4 and 6), transport-independent.

Two-approver holds (owner decision 2026-09-29, DECISIONS row 4): a hold that needs two approvals
(four-eyes threshold) collects them across separate requests from two DISTINCT JWT subjects with
role approver. The first approval is audited and kept in memory (``DecisionBook.pending``) until
the second distinct approver decides, someone rejects, or the hold expires; only the second
approval calls ``Aegis.ResolveHold``. The same subject approving twice is refused
(``duplicate_approver``); a REJECT from any approver, including the first one, is final. Each
approval is attested to Aegis with this service's attestor key (``attest.py``: the OIDC subject,
not a per-person key). Every step is audited in the hash-chained audit log.

Released holds carry the debate context retained in Zone C (DECISIONS row 7, ``retained.py``)
when there is one, else a snapshot built from the held signal only, labelled as such.

Every attempt is audited before the response, denials included; if that audit write fails, the
decision is refused (contract 6.6).
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import grpc
import structlog

from .attest import ApprovalAttestor, Attestation, SubjectNotAttestable
from .auth import Operator
from .holds import (
    FINAL_STATUSES,
    DecisionBook,
    FinalRecord,
    PendingApproval,
    hold_json,
    required_approvals,
)
from .retained import Retained, RetainedContextError, RetainedContexts

log = structlog.get_logger("hitl_backend.service")

HOLD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MIN_REASON, MAX_REASON = 10, 1000
_NOTE_CHARS = 480
_DECISION_APPROVED = 1  # aegis.proto DecisionStatus
_RETAINED_STATUS = {
    "retained_context_unavailable": 503,
    "retained_context_integrity": 409,
    "retained_context_conflict": 409,
    "retained_context_mismatch": 409,
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message[:500]


class AuditSink(Protocol):
    def record(self, event_type: str, actor: str, payload: dict[str, object]) -> Any: ...


class Holds(Protocol):
    identity: str

    def list(self) -> list[Any]: ...
    def get(self, hold_id: str) -> Any: ...
    def resolve(self, **fields: Any) -> Any: ...


class Relay(Protocol):
    def execute(self, request: Any) -> Any: ...


@dataclass(frozen=True)
class Policy:
    quantity_threshold: Decimal
    notional_threshold_usd: Decimal | None
    cooling_period_s: float


class HitlService:
    def __init__(
        self,
        *,
        holds: Holds,
        audit: AuditSink,
        pb: dict[str, Any],
        policy: Policy,
        relay: Relay | None,
        attestor: ApprovalAttestor | None = None,
        retained: RetainedContexts | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._holds = holds
        self._audit = audit
        self._pb = pb
        self._policy = policy
        self._relay = relay
        self._attestor = attestor
        self._retained = retained
        self._clock = clock_ns
        self._book = DecisionBook()

    # ---------------------------------------------------------------------------- reads

    def list_pending(self) -> dict[str, Any]:
        held = self._call(self._holds.list)
        with self._book.lock:
            return {"holds": [self._pending_json(h) for h in held]}

    def get(self, hold_id: str) -> dict[str, Any]:
        _check_hold_id(hold_id)
        with self._book.lock:
            final = self._book.finals.get(hold_id)
        if final is not None:
            return final.hold
        held = self._fetch(hold_id)
        with self._book.lock:
            return self._pending_json(held)

    # ------------------------------------------------------------------------ decisions

    def decide(self, operator: Operator, hold_id: str, body: Any) -> tuple[int, dict[str, Any]]:
        """Returns (http status, json body). Raises ApiError only for request-shape errors."""
        decision, reason, request_id = _parse_body(body)
        _check_hold_id(hold_id)
        attempt = {
            "hold_id": hold_id,
            "decision": decision,
            "reason": reason,
            "client_request_id": request_id,
            "role": operator.role,
            "amr": list(operator.amr),
        }
        # Keyed per operator: a request id is only meaningful to the person who sent it, and
        # another operator's reply must never be returned to them.
        key = f"{operator.sub}\x00{request_id}"
        with self._book.lock:
            replay = self._book.replies.get(key)
            if replay is not None:
                self._audit_or_refuse("hitl.decision.replayed", operator.sub, attempt)
                return replay
            try:
                reply = self._decide_locked(operator, hold_id, decision, reason, attempt)
            except ApiError as err:
                reply = (err.status, _error(err.code, err.message))
                if err.code != "audit_unavailable":
                    self._audit_quietly(
                        "hitl.decision.denied", operator.sub, {**attempt, "code": err.code}
                    )
            if reply[0] < 500:  # never cache an unknown outcome
                self._book.replies[key] = reply
            return reply

    def _decide_locked(
        self, operator: Operator, hold_id: str, decision: str, reason: str, attempt: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if operator.role != "approver":
            raise ApiError(403, "role_not_allowed", "only approvers can decide")
        if hold_id in self._book.finals:
            raise ApiError(409, "already_final", "this hold already has a final decision")
        try:
            held = self._fetch(hold_id)
        except ApiError as err:
            if err.code == "not_found":
                self._drop_pending(hold_id, "hold_gone")
            raise
        now = self._clock()
        expires = min(held.signal.valid_until_ns, held.decision.hold_expires_at_ns)
        if now >= expires:
            self._drop_pending(hold_id, "hold_expired")
            raise ApiError(410, "expired", "the hold has expired")
        required = self._required(held)
        approve = decision == "APPROVE"
        pending = self._pending_for(hold_id, now)
        # The reverse-guardrail distress classifier is out of scope (owner decision 2026-09-29):
        # the field is always 0.0 and the audit record says it was not scored.
        score = 0.0
        cooling = self._policy.cooling_period_s > 0
        if approve:
            if pending is not None and pending.sub == operator.sub:
                raise ApiError(
                    409,
                    "duplicate_approver",
                    "you already approved this hold; a different approver must give the second",
                )
            waited_s = (now - held.decision.decided_at_ns) / 1e9
            if cooling and waited_s < self._policy.cooling_period_s:
                raise ApiError(422, "cooling_period", "the cooling period has not elapsed")
            if required > 1 and pending is None:
                return self._first_approval(operator, held, reason, attempt, now, expires)
        first = pending if approve else None
        retained = self._load_retained(held) if approve else None
        second: Attestation | None = None
        if first is not None:
            second = self._attest(held, operator, now)
        attempt = {
            **attempt,
            "required_approvals": required,
            "first_approver": pending.sub if pending is not None else None,
            "attested_by": (
                self._attestor.attestor_id
                if second is not None and self._attestor is not None
                else None
            ),
        }
        self._audit_or_refuse("hitl.decision.attempt", operator.sub, attempt)
        note = self._note(operator, reason, first)
        try:
            fields = self._resolve_fields(held, approve, note, operator, first, second)
            result = self._holds.resolve(**fields)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                self._drop_pending(hold_id, "hold_gone")
            raise _map_resolve_error(exc) from exc
        self._book.pending.pop(hold_id, None)
        status = _hitl_status(result.decision, approve)
        approval = {
            "approverSub": operator.sub,
            "decision": decision,
            "reason": reason,
            "decidedAtNs": str(now),
        }
        approvals = ([pending.json()] if pending is not None else []) + [approval]
        hold = hold_json(held, self._pb, status=status, required=required, approvals=approvals)
        override = self._override_record(operator, reason, now, score, cooling, approve, first)
        execution = None
        if status == "APPROVED":
            execution = self._relay_release(result, held, override, retained)
            hold["execution"] = execution
        self._book.finals[hold_id] = FinalRecord(hold)
        self._audit_quietly(
            "hitl.decision.result",
            operator.sub,
            {
                **attempt,
                "hitl_status": status,
                "approvers": [a["approverSub"] for a in approvals if a["decision"] == "APPROVE"],
                "aegis_decision": int(result.decision),
                "aegis_reasons": [int(r) for r in result.reasons],
                "execution": execution,
                "hitl_override": {
                    "operator_id": override.operator_id,
                    "override_timestamp_ns": now,
                    "decision": override.decision,
                    "distress_score": score,
                    "distress_scored": False,
                    "distress_classifier": "out_of_scope",
                    "cooling_period_enforced": cooling,
                },
            },
        )
        return 200, hold

    def _first_approval(
        self,
        operator: Operator,
        held: Any,
        reason: str,
        attempt: dict[str, Any],
        now: int,
        expires: int,
    ) -> tuple[int, dict[str, Any]]:
        """Record the first of two approvals. Nothing reaches Aegis yet."""
        attestation = self._attest(held, operator, now)
        self._audit_or_refuse(
            "hitl.decision.first_approval",
            operator.sub,
            {
                **attempt,
                "required_approvals": 2,
                "pending_until_ns": expires,
                "attested_by": self._attestor.attestor_id if self._attestor else None,
                "idp_issuer": self._attestor.issuer if self._attestor else None,
            },
        )
        pending = PendingApproval(operator.sub, reason, now, expires, attestation)
        hold_id = held.decision.hold_id
        self._book.pending[hold_id] = pending
        hold = hold_json(held, self._pb, status="PENDING", required=2, approvals=[pending.json()])
        return 200, hold

    # --------------------------------------------------------------------------- helpers

    def _attest(self, held: Any, operator: Operator, now: int) -> Attestation | None:
        if self._attestor is None:
            return None  # dev only: config requires an attestor in production
        try:
            return self._attestor.attest(held.decision.hold_id, True, operator.sub, now)
        except SubjectNotAttestable as exc:
            raise ApiError(422, "subject_unusable", str(exc)) from exc

    def _resolve_fields(
        self,
        held: Any,
        approve: bool,
        note: str,
        operator: Operator,
        first: PendingApproval | None,
        second: Attestation | None,
    ) -> dict[str, Any]:
        """``operator_id`` is this service's certificate identity (set by ``AegisHolds``), so the
        two humans travel as the attested first and second approvals (see ``attest.py``)."""
        fields: dict[str, Any] = {
            "hold_id": held.decision.hold_id,
            "approve": approve,
            "note": note,
            "reverse_guardrail_distress_score": 0.0,
            "cooling_period_enforced": self._policy.cooling_period_s > 0,
        }
        if first is None:
            return fields
        fields["second_approver_id"] = operator.sub
        auth = self._pb["aegis_pb2"].Authorization
        if second is not None:
            fields["second_approval"] = auth(**second.as_fields())
        if first.attestation is not None:
            fields["first_approval"] = auth(**first.attestation.as_fields())
        return fields

    @staticmethod
    def _note(operator: Operator, reason: str, first: PendingApproval | None) -> str:
        if first is None:
            return f"{operator.sub}: {reason}"[:_NOTE_CHARS]
        return f"{first.sub} + {operator.sub}: {reason}"[:_NOTE_CHARS]

    def _pending_for(self, hold_id: str, now: int) -> PendingApproval | None:
        pending = self._book.pending.get(hold_id)
        if pending is not None and now >= pending.expires_at_ns:
            self._drop_pending(hold_id, "hold_expired")
            return None
        return pending

    def _drop_pending(self, hold_id: str, why: str) -> None:
        pending = self._book.pending.pop(hold_id, None)
        if pending is not None:
            self._audit_quietly(
                "hitl.first_approval.lapsed",
                pending.sub,
                {"hold_id": hold_id, "why": why, "approved_at_ns": pending.decided_at_ns},
            )

    def _load_retained(self, held: Any) -> Retained | None:
        if self._retained is None:
            return None
        try:
            return self._retained.load(held)
        except RetainedContextError as err:
            log.critical("retained_context_refused", hold_id=held.decision.hold_id, code=err.code)
            raise ApiError(_RETAINED_STATUS.get(err.code, 503), err.code, err.message) from err

    def _call(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except grpc.RpcError as exc:
            raise ApiError(
                503, "aegis_unavailable", f"Aegis call failed: {exc.code().name}"
            ) from exc

    def _fetch(self, hold_id: str) -> Any:
        try:
            return self._holds.get(hold_id)
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                raise ApiError(404, "not_found", "unknown or expired hold") from exc
            raise ApiError(
                503, "aegis_unavailable", f"Aegis call failed: {exc.code().name}"
            ) from exc

    def _required(self, held: Any) -> int:
        return required_approvals(
            held.signal,
            quantity_threshold=self._policy.quantity_threshold,
            notional_threshold_usd=self._policy.notional_threshold_usd,
        )

    def _pending_json(self, held: Any) -> dict[str, Any]:
        """Call with the book lock held."""
        pending = self._pending_for(held.decision.hold_id, self._clock())
        approvals = [pending.json()] if pending is not None else []
        return hold_json(
            held, self._pb, status="PENDING", required=self._required(held), approvals=approvals
        )

    def _override_record(
        self,
        operator: Operator,
        reason: str,
        now: int,
        score: float,
        cooling: bool,
        approve: bool,
        first: PendingApproval | None,
    ) -> Any:
        # The manifest's HITL record has one operator field: name both approvers there.
        operator_id = operator.sub if first is None else f"{first.sub}+{operator.sub}"
        return self._pb["compliance_manifest_pb2"].HITLOverrideRecord(
            operator_id=operator_id,
            override_timestamp_ns=now,
            override_text=reason,
            reverse_guardrail_distress_score=score,
            cooling_period_enforced=cooling,
            decision="approved" if approve else "rejected",
        )

    def _relay_release(
        self, result: Any, held: Any, override: Any, retained: Retained | None
    ) -> dict[str, Any]:
        """Send the released decision to execution-motor (ExecuteWithContext) with the debate
        context retained in Zone C (DECISIONS row 7), or, when none was retained, a snapshot
        built from the held signal only; ``model_versions`` says which."""
        if self._relay is None:
            return {"relayed": False, "detail": "no execution-motor configured (dev)"}
        motor, snap = self._pb["execution_motor_pb2"], self._pb["market_snapshot_pb2"]
        signal = held.signal
        if retained is not None:
            context = motor.ExecutionContext()
            context.CopyFrom(retained.context)
            context.hitl_override.CopyFrom(override)
            context.model_versions["snapshot-source"] = "retained-debate-context"
            context.model_versions["retained-context-audit-seq"] = str(retained.audit_seq)
        else:
            snapshot = snap.MarketSnapshot(
                symbol=signal.symbol,
                regime=signal.regime,
                ingestion_timestamp_ns=signal.created_at_ns,
            )
            context = motor.ExecutionContext(
                snapshot=snapshot,
                signal=signal,
                judge_synthesis=signal.debate_summary,
                hitl_override=override,
            )
            context.model_versions["snapshot-source"] = "hold-signal-only"
            context.model_versions["retained-context"] = "absent"
        context.model_versions["hitl-backend"] = "ali-156"
        try:
            ack = self._relay.execute(motor.ExecuteRequest(decision=result, context=context))
        except grpc.RpcError as exc:
            log.error("release_relay_failed", hold_signal=signal.signal_id, code=exc.code().name)
            return {"relayed": False, "detail": f"execution-motor call failed: {exc.code().name}"}
        return {
            "relayed": True,
            "accepted": bool(ack.accepted),
            "status": ack.status,
            "rejectReason": ack.reject_reason,
            "detail": ack.detail[:300],
            "snapshotSource": context.model_versions["snapshot-source"],
        }

    def _audit_or_refuse(self, event: str, actor: str, payload: dict[str, Any]) -> None:
        try:
            self._audit.record(event, actor, payload)
        except Exception as exc:  # noqa: BLE001 - no audit, no decision (contract 6.6)
            log.error("audit_failed", audit_event=event, error=type(exc).__name__)
            raise ApiError(
                503, "audit_unavailable", "audit log unavailable; decision refused"
            ) from exc

    def _audit_quietly(self, event: str, actor: str, payload: dict[str, Any]) -> None:
        try:
            self._audit.record(event, actor, payload)
        except Exception as exc:  # noqa: BLE001 - the attempt itself was already audited
            log.critical("audit_failed_after_action", audit_event=event, error=type(exc).__name__)


def _check_hold_id(hold_id: str) -> None:
    if HOLD_ID.fullmatch(hold_id) is None:
        raise ApiError(404, "not_found", "unknown hold")


def _parse_body(body: Any) -> tuple[str, str, str]:
    if not isinstance(body, dict):
        raise ApiError(422, "invalid_request", "body must be a JSON object")
    decision, reason, request_id = (
        body.get("decision"),
        body.get("reason"),
        body.get("clientRequestId"),
    )
    if decision not in ("APPROVE", "REJECT"):
        raise ApiError(422, "invalid_request", "decision must be APPROVE or REJECT")
    if not isinstance(reason, str):
        raise ApiError(422, "reason_missing", "reason is required")
    reason = reason.strip()
    if not MIN_REASON <= len(reason) <= MAX_REASON or _CONTROL.search(reason):
        raise ApiError(422, "reason_missing", f"reason must be {MIN_REASON}..{MAX_REASON} chars")
    try:
        uuid.UUID(str(request_id))
    except ValueError as exc:
        raise ApiError(422, "invalid_request", "clientRequestId must be a UUID") from exc
    return decision, reason, str(request_id)


def _hitl_status(aegis_decision: int, approve: bool) -> str:
    if not approve:
        return "REJECTED"
    return "APPROVED" if aegis_decision == _DECISION_APPROVED else "RELEASE_DENIED"


def _map_resolve_error(exc: grpc.RpcError) -> ApiError:
    code = exc.code()
    detail = (exc.details() or "")[:300]
    if code == grpc.StatusCode.NOT_FOUND:
        return ApiError(410, "expired", "the hold expired or is unknown to Aegis")
    if code == grpc.StatusCode.FAILED_PRECONDITION:
        return ApiError(422, "aegis_refused", f"Aegis refused the release: {detail}")
    if code == grpc.StatusCode.PERMISSION_DENIED:
        # Also a bad or stale approval attestation (Aegis verifies both approvals).
        return ApiError(503, "aegis_permission", f"Aegis refused this service: {detail}")
    return ApiError(503, "aegis_unavailable", f"outcome unknown ({code.name}); reload to verify")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message[:500]}}


__all__ = ["FINAL_STATUSES", "ApiError", "HitlService", "Policy"]
