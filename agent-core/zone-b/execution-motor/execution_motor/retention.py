"""Retention of a HELD signal's debate context in Zone C (owner decision 2026-09-29, row 7).

When Aegis holds a signal for a human (``DECISION_HELD_FOR_HUMAN``), cognitive-core sends the same
``ExecuteRequest`` it would have sent for an approved one (the held ``AegisDecision`` plus the full
``ExecutionContext``: snapshot, model versions, Blue/Red/Judge texts, compression summaries) to
``ExecutionMotor.RetainHeldContext``. The motor writes it as ONE append-only, hash-chained audit
record (``hold.context.retained``, ``afe_audit`` as ``afe_audit_app``), keyed by ``hold_id`` and
``signal_id``. Nothing is executed and nothing is sent to a broker.

Why the motor: it is the component that already receives this context from cognitive-core over
mTLS and already writes compliance records to the audit database (``zone-bc-audit``). Routing it
here needs no new network route; cognitive-core (Zone A) still never touches the audit store.

Trust: as for ``ExecuteWithContext``, the context is not signed (held decisions carry no
attestation). The record is bound to the hold on READ: hitl-backend uses it only if its hold_id,
signal_id, decision time and its ``TradeSignal`` equal Aegis's own copy of the held signal.

Retention period: none is enforced and there is no deletion job; the audit table is append-only.
TODO(owner): the audit retention period needs legal confirmation (issue #32).
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import structlog

_log = structlog.get_logger("execution_motor.retention")

HELD_CONTEXT_EVENT: Final = "hold.context.retained"
HELD_CONTEXT_SCHEMA: Final = "afe-held-context-v1"
#: Serialized ExecutionContext limit. The audit canonical form is capped at 1 MiB and base64
#: grows the bytes by 4/3, so 512 KiB leaves room for the rest of the payload.
MAX_CONTEXT_BYTES: Final = 512 * 1024
_DECISION_HELD: Final = 3  # aegis.proto DecisionStatus.DECISION_HELD_FOR_HUMAN
_HOLD_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DETAIL_CHARS: Final = 200


class AuditSink(Protocol):
    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> Any: ...


@dataclass(frozen=True)
class RetainResult:
    retained: bool
    audit_seq: int = 0
    record_hash: str = ""
    detail: str = ""


def check_held(request: Any) -> str | None:
    """None if ``request`` is a HELD decision with a context describing exactly that signal."""
    decision = request.decision
    if int(decision.decision) != _DECISION_HELD:
        return "decision is not HELD_FOR_HUMAN"
    if _HOLD_ID.fullmatch(decision.hold_id) is None:
        return "held decision has no usable hold_id"
    if not request.HasField("context"):
        return "request has no context"
    context = request.context
    if not (context.HasField("signal") and context.HasField("snapshot")):
        return "context lacks signal or snapshot"
    if not decision.signal_id or context.signal.signal_id != decision.signal_id:
        return "context signal_id differs from the held decision"
    if context.signal.symbol != context.snapshot.symbol:
        return "context symbol differs between signal and snapshot"
    return None


def build_payload(request: Any) -> dict[str, object]:
    """The audit payload. The proto messages are kept byte-exact (deterministic serialisation,
    base64) with their sha256; readable copies of the keys sit next to them. No floats (the
    audit canonical form refuses them): the snapshot's doubles live only inside the bytes."""
    decision, context = request.decision, request.context
    context_bytes = context.SerializeToString(deterministic=True)
    if len(context_bytes) > MAX_CONTEXT_BYTES:
        raise ValueError(f"context is {len(context_bytes)} bytes, limit {MAX_CONTEXT_BYTES}")
    decision_bytes = decision.SerializeToString(deterministic=True)
    return {
        "schema": HELD_CONTEXT_SCHEMA,
        "hold_id": decision.hold_id,
        "signal_id": decision.signal_id,
        "symbol": context.signal.symbol,
        "strategy_id": context.signal.strategy_id,
        "aegis_decided_at_ns": int(decision.decided_at_ns),
        "hold_expires_at_ns": int(decision.hold_expires_at_ns),
        "model_versions": {str(k): str(v) for k, v in context.model_versions.items()},
        "context_proto_b64": base64.b64encode(context_bytes).decode("ascii"),
        "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
        "decision_proto_b64": base64.b64encode(decision_bytes).decode("ascii"),
        "decision_sha256": hashlib.sha256(decision_bytes).hexdigest(),
        "retention": "audit retention period; TODO(owner): legal confirmation (issue #32)",
    }


class HeldContextRetainer:
    """Validate, then append one audit record. Never raises: every failure is a ``RetainResult``
    with ``retained=False`` (the caller logs it; the hold then falls back to a snapshot built
    from the held signal only, labelled as such by hitl-backend)."""

    def __init__(self, audit: AuditSink, *, actor: str) -> None:
        self._audit = audit
        self._actor = actor

    def retain(self, request: Any) -> RetainResult:
        refusal = check_held(request)
        if refusal is not None:
            return RetainResult(False, detail=refusal)
        try:
            payload = build_payload(request)
            record = self._audit.record(HELD_CONTEXT_EVENT, self._actor, payload)
        except Exception as exc:  # noqa: BLE001 - not retained is an answer, never a crash
            detail = f"not retained: {type(exc).__name__}: {exc}"[:_DETAIL_CHARS]
            _log.error("held_context_not_retained", hold_id=request.decision.hold_id, error=detail)
            return RetainResult(False, detail=detail)
        seq = int(getattr(record, "seq", 0) or 0)
        digest = str(getattr(record, "hash", "") or "")
        _log.info("held_context_retained", hold_id=request.decision.hold_id, audit_seq=seq)
        return RetainResult(True, audit_seq=seq, record_hash=digest)


def build_retainer(env: Mapping[str, str], *, actor: str) -> HeldContextRetainer:
    """Wire the real audit logger (POSTGRES_* as afe_audit_app). Imports happen here only."""
    from afe_audit import AuditLogger  # type: ignore[import-not-found]
    from afe_audit.db import DsnConnectionSource  # type: ignore[import-not-found]

    return HeldContextRetainer(AuditLogger(DsnConnectionSource.from_env(env)), actor=actor)
