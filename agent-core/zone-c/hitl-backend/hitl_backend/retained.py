"""Load the debate context retained with a hold (owner decision 2026-09-29, DECISIONS row 7).

execution-motor writes one ``hold.context.retained`` audit record per HELD signal (cognitive-core
sends it over the existing mTLS channel; see ``execution_motor/retention.py``). When a human
releases the hold, this module reads it back through ``afe_audit.RecordLookup``, which re-checks
the record's hash and its link to the previous record, and then binds it to Aegis's own copy of
the held signal. Only a record that passes every check is used:

* ``found``: the context the debate actually saw goes to execution-motor (``snapshot-source:
  retained-debate-context``);
* ``absent``: nothing was retained (cognitive-core could not reach the motor, retention not
  configured, or a hold from before this change); the caller falls back to a snapshot built from
  the held signal only, labelled ``hold-signal-only``;
* anything else (tampered record, two different records for one hold, a record that does not
  describe this hold, or an unreadable store) raises ``RetainedContextError``, and the release is
  refused. A human can still reject the hold.

No retention period is enforced here and nothing is deleted. TODO(owner): the audit retention
period needs legal confirmation (issue #32).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol

EVENT = "hold.context.retained"
SCHEMA = "afe-held-context-v1"
_MAX_RECORDS = 4


class RetainedContextError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RecordSource(Protocol):
    """``afe_audit.RecordLookup``: ``find`` returns records with ``seq``, ``hash``, ``payload``."""

    def find(self, event_type: str, key: str, value: str, *, limit: int = 2) -> list[Any]: ...


@dataclass(frozen=True)
class Retained:
    context: Any  # execution_motor_pb2.ExecutionContext
    audit_seq: int
    record_hash: str


class RetainedContexts:
    def __init__(
        self,
        source: RecordSource,
        pb: dict[str, Any],
        *,
        integrity_errors: tuple[type[BaseException], ...] = (),
    ) -> None:
        """``integrity_errors``: the source's "record was altered" exceptions
        (``afe_audit.AuditIntegrityError``); any other failure counts as unavailable."""
        self._source = source
        self._pb = pb
        self._integrity_errors = integrity_errors

    def load(self, held: Any) -> Retained | None:
        """The verified context retained for ``held`` (an Aegis ``HeldSignal``), or None."""
        hold_id = held.decision.hold_id
        try:
            records = self._source.find(EVENT, "hold_id", hold_id, limit=_MAX_RECORDS)
        except self._integrity_errors as exc:
            raise RetainedContextError(
                "retained_context_integrity",
                "the retained context failed its audit integrity check",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - an unreadable store: never guess
            raise RetainedContextError(
                "retained_context_unavailable",
                f"cannot read the retained context ({type(exc).__name__})",
            ) from exc
        if not records:
            return None
        parsed = [self._parse(record, held) for record in records]
        digests = {digest for _, digest in parsed}
        if len(digests) != 1:
            raise RetainedContextError(
                "retained_context_conflict", "different contexts were retained for this hold"
            )
        context, _ = parsed[0]
        first = records[0]
        return Retained(context=context, audit_seq=int(first.seq), record_hash=str(first.hash))

    def _parse(self, record: Any, held: Any) -> tuple[Any, str]:
        payload = record.payload
        mismatch = RetainedContextError(
            "retained_context_mismatch", "the retained context does not describe this hold"
        )
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise mismatch
        decision, signal = held.decision, held.signal
        try:
            raw_context = base64.b64decode(str(payload["context_proto_b64"]), validate=True)
            raw_decision = base64.b64decode(str(payload["decision_proto_b64"]), validate=True)
        except (KeyError, binascii.Error) as exc:
            raise mismatch from exc
        if hashlib.sha256(raw_context).hexdigest() != payload.get("context_sha256"):
            raise mismatch
        if hashlib.sha256(raw_decision).hexdigest() != payload.get("decision_sha256"):
            raise mismatch
        try:
            context = self._pb["execution_motor_pb2"].ExecutionContext.FromString(raw_context)
            retained_decision = self._pb["aegis_pb2"].AegisDecision.FromString(raw_decision)
        except Exception as exc:  # noqa: BLE001 - protobuf DecodeError and friends
            raise mismatch from exc
        same_hold = (
            payload.get("hold_id") == decision.hold_id == retained_decision.hold_id
            and payload.get("signal_id") == decision.signal_id == retained_decision.signal_id
            and retained_decision.decided_at_ns == decision.decided_at_ns
            and context.HasField("signal")
            and context.HasField("snapshot")
            and context.signal == signal  # Aegis's own copy of what was held
            and context.snapshot.symbol == signal.symbol
        )
        if not same_hold:
            raise mismatch
        return context, str(payload["context_sha256"])
