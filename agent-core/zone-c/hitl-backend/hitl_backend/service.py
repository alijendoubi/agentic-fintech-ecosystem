"""Hold listing and operator decisions (contract sections 4 and 6), transport-independent.

Scope (owner decision, ALI-156): single-approver holds. A hold that needs two approvers (four-eyes
threshold, or Aegis's own ``hold_requires_second_approver``) cannot be APPROVED here: the second
approver would have to sign an ``afe-hold-v1`` approval with their own key (ALI-164), and no
signing method exists yet. Such approvals are refused (``second_approver_signing_unavailable``);
REJECT still works (one rejection is final).

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

from .auth import Operator
from .holds import FINAL_STATUSES, DecisionBook, FinalRecord, hold_json, required_approvals

log = structlog.get_logger("hitl_backend.service")

HOLD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MIN_REASON, MAX_REASON = 10, 1000
_NOTE_CHARS = 480
_DECISION_APPROVED = 1  # aegis.proto DecisionStatus


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
    unscored_dev: bool


class HitlService:
    def __init__(
        self,
        *,
        holds: Holds,
        audit: AuditSink,
        pb: dict[str, Any],
        policy: Policy,
        relay: Relay | None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._holds = holds
        self._audit = audit
        self._pb = pb
        self._policy = policy
        self._relay = relay
        self._clock = clock_ns
        self._book = DecisionBook()

    # ---------------------------------------------------------------------------- reads

    def list_pending(self) -> dict[str, Any]:
        return {"holds": [self._pending_json(h) for h in self._call(self._holds.list)]}

    def get(self, hold_id: str) -> dict[str, Any]:
        _check_hold_id(hold_id)
        with self._book.lock:
            final = self._book.finals.get(hold_id)
        if final is not None:
            return final.hold
        return self._pending_json(self._fetch(hold_id))

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
                self._audit_or_refuse("hitl.decision.replayed", operator, attempt)
                return replay
            try:
                reply = self._decide_locked(operator, hold_id, decision, reason, attempt)
            except ApiError as err:
                reply = (err.status, _error(err.code, err.message))
                if err.code != "audit_unavailable":
                    self._audit_quietly(
                        "hitl.decision.denied", operator, {**attempt, "code": err.code}
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
        held = self._fetch(hold_id)
        now = self._clock()
        if now >= min(held.signal.valid_until_ns, held.decision.hold_expires_at_ns):
            raise ApiError(410, "expired", "the hold has expired")
        required = self._required(held)
        approve = decision == "APPROVE"
        score = 0.0
        cooling = self._policy.cooling_period_s > 0
        if approve:
            if required > 1:
                raise ApiError(
                    422,
                    "second_approver_signing_unavailable",
                    "this hold needs two approvers; signed second approvals are not implemented",
                )
            if not self._policy.unscored_dev:
                raise ApiError(
                    422,
                    "distress_classifier_unavailable",
                    "no reverse-guardrail distress classifier is configured",
                )
            waited_s = (now - held.decision.decided_at_ns) / 1e9
            if cooling and waited_s < self._policy.cooling_period_s:
                raise ApiError(422, "cooling_period", "the cooling period has not elapsed")
        self._audit_or_refuse("hitl.decision.attempt", operator, attempt)
        note = f"{operator.sub}: {reason}"[:_NOTE_CHARS]
        try:
            result = self._holds.resolve(
                hold_id=hold_id,
                approve=approve,
                note=note,
                reverse_guardrail_distress_score=score,
                cooling_period_enforced=cooling,
            )
        except grpc.RpcError as exc:
            raise _map_resolve_error(exc) from exc
        status = _hitl_status(result.decision, approve)
        approval = {
            "approverSub": operator.sub,
            "decision": decision,
            "reason": reason,
            "decidedAtNs": str(now),
        }
        hold = hold_json(held, self._pb, status=status, required=required, approvals=[approval])
        override = self._override_record(operator, reason, now, score, cooling, approve)
        execution = None
        if status == "APPROVED":
            execution = self._relay_release(result, held, override)
            hold["execution"] = execution
        self._book.finals[hold_id] = FinalRecord(hold)
        self._audit_quietly(
            "hitl.decision.result",
            operator,
            {
                **attempt,
                "hitl_status": status,
                "aegis_decision": int(result.decision),
                "aegis_reasons": [int(r) for r in result.reasons],
                "execution": execution,
                "hitl_override": {
                    "operator_id": operator.sub,
                    "override_timestamp_ns": now,
                    "decision": override.decision,
                    "distress_score": score,
                    "distress_scored": False,
                    "cooling_period_enforced": cooling,
                },
            },
        )
        return 200, hold

    # --------------------------------------------------------------------------- helpers

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
        return hold_json(
            held, self._pb, status="PENDING", required=self._required(held), approvals=[]
        )

    def _override_record(
        self, operator: Operator, reason: str, now: int, score: float, cooling: bool, approve: bool
    ) -> Any:
        return self._pb["compliance_manifest_pb2"].HITLOverrideRecord(
            operator_id=operator.sub,
            override_timestamp_ns=now,
            override_text=reason,
            reverse_guardrail_distress_score=score,
            cooling_period_enforced=cooling,
            decision="approved" if approve else "rejected",
        )

    def _relay_release(self, result: Any, held: Any, override: Any) -> dict[str, Any]:
        """Send the released decision to execution-motor (ExecuteWithContext). The debate's market
        snapshot is not retained for held signals, so the context snapshot carries only what the
        held signal itself says; ``model_versions`` labels it (TODO(owner): retain the snapshot)."""
        if self._relay is None:
            return {"relayed": False, "detail": "no execution-motor configured (dev)"}
        motor, snap = self._pb["execution_motor_pb2"], self._pb["market_snapshot_pb2"]
        signal = held.signal
        snapshot = snap.MarketSnapshot(
            symbol=signal.symbol, regime=signal.regime, ingestion_timestamp_ns=signal.created_at_ns
        )
        context = motor.ExecutionContext(
            snapshot=snapshot,
            signal=signal,
            judge_synthesis=signal.debate_summary,
            hitl_override=override,
        )
        context.model_versions["hitl-backend"] = "ali-156"
        context.model_versions["snapshot-source"] = "hold-signal-only"
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
        }

    def _audit_or_refuse(self, event: str, operator: Operator, payload: dict[str, Any]) -> None:
        try:
            self._audit.record(event, operator.sub, payload)
        except Exception as exc:  # noqa: BLE001 - no audit, no decision (contract 6.6)
            log.error("audit_failed", audit_event=event, error=type(exc).__name__)
            raise ApiError(
                503, "audit_unavailable", "audit log unavailable; decision refused"
            ) from exc

    def _audit_quietly(self, event: str, operator: Operator, payload: dict[str, Any]) -> None:
        try:
            self._audit.record(event, operator.sub, payload)
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
        return ApiError(503, "aegis_permission", "this service is not allowed to resolve holds")
    return ApiError(503, "aegis_unavailable", f"outcome unknown ({code.name}); reload to verify")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message[:500]}}


__all__ = ["FINAL_STATUSES", "ApiError", "HitlService", "Policy"]
