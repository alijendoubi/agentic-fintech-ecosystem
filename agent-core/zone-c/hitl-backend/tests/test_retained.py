"""Released holds carry the debate context retained in Zone C (DECISIONS row 7), bound to Aegis's
copy of the held signal; only an absent record falls back to the held-signal-only snapshot."""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hitl_backend.auth import Operator
from hitl_backend.retained import EVENT, SCHEMA, RetainedContexts
from hitl_backend.service import HitlService
from tests.fakes import HOLD_ID, NOW_NS, FakeAudit, FakeHolds, FakeRelay
from tests.fakes import held_signal as make_held
from tests.test_service import policy

APPROVER = Operator(sub="alice", role="approver", amr=("pwd", "mfa"))


class Tampered(Exception):
    """Stands in for afe_audit.AuditIntegrityError."""


@dataclass(frozen=True)
class Record:
    seq: int
    hash: str
    payload: dict[str, Any]


class FakeRecords:
    def __init__(self, records: list[Record] | None = None, error: Exception | None = None):
        self.records = records or []
        self.error = error
        self.queries: list[tuple[str, str, str]] = []

    def find(self, event_type: str, key: str, value: str, *, limit: int = 2) -> list[Any]:
        self.queries.append((event_type, key, value))
        if self.error is not None:
            raise self.error
        return [r for r in self.records if r.payload.get(key) == value][:limit]


def debate_context(pb: dict[str, Any], held: Any) -> Any:
    """What cognitive-core sends for a held signal (see sinks.build_execute_request)."""
    snapshot = pb["market_snapshot_pb2"].MarketSnapshot(
        symbol="AAPL",
        mid_price=190.42,
        z_score=-2.1,
        realized_volatility=0.31,
        regime=pb["market_snapshot_pb2"].TRENDING_BULL,
        ingestion_timestamp_ns=NOW_NS - 2_000_000_000,
    )
    context = pb["execution_motor_pb2"].ExecutionContext(
        snapshot=snapshot,
        signal=held.signal,
        blue_node_thesis="BUY: momentum",
        red_node_challenge="earnings risk",
        judge_synthesis="Judge sided BUY",
    )
    context.model_versions["cognitive-core/judge"] = "judge-model"
    return context


def record_for(held: Any, context: Any, *, seq: int = 7, **over: Any) -> Record:
    """Mirror of execution_motor.retention.build_payload."""
    raw = context.SerializeToString(deterministic=True)
    raw_decision = held.decision.SerializeToString(deterministic=True)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "hold_id": held.decision.hold_id,
        "signal_id": held.decision.signal_id,
        "context_proto_b64": base64.b64encode(raw).decode(),
        "context_sha256": hashlib.sha256(raw).hexdigest(),
        "decision_proto_b64": base64.b64encode(raw_decision).decode(),
        "decision_sha256": hashlib.sha256(raw_decision).hexdigest(),
    }
    payload.update(over)
    return Record(seq=seq, hash="cd" * 32, payload=payload)


class Rig:
    def __init__(self, pb: dict[str, Any], source: FakeRecords, **pol: Any) -> None:
        self.pb = pb
        self.held = make_held(pb)
        self.holds = FakeHolds(pb, self.held)
        self.audit = FakeAudit()
        self.relay = FakeRelay(pb)
        self.source = source
        self.service = HitlService(
            holds=self.holds,
            audit=self.audit,
            pb=pb,
            policy=policy(**pol),
            relay=self.relay,
            retained=RetainedContexts(source, pb, integrity_errors=(Tampered,)),
            clock_ns=lambda: NOW_NS,
        )

    def decide(self, decision: str = "APPROVE", request_id: str | None = None) -> tuple[int, Any]:
        body = {
            "decision": decision,
            "reason": "size is fine for this book",
            "clientRequestId": request_id or str(uuid.uuid4()),
        }
        return self.service.decide(APPROVER, HOLD_ID, body)


def code(reply: tuple[int, Any]) -> tuple[int, str]:
    return reply[0], reply[1]["error"]["code"]


def test_a_released_hold_carries_the_retained_debate_context(pb: dict[str, Any]) -> None:
    held = make_held(pb)
    context = debate_context(pb, held)
    rig = Rig(pb, FakeRecords([record_for(held, context)]))
    status, hold = rig.decide()
    assert status == 200 and hold["hitlStatus"] == "APPROVED"
    assert hold["execution"]["snapshotSource"] == "retained-debate-context"
    assert rig.source.queries == [(EVENT, "hold_id", HOLD_ID)]
    (request,) = rig.relay.requests
    sent = request.context
    assert sent.snapshot == context.snapshot  # the market data the debate saw
    assert (sent.blue_node_thesis, sent.red_node_challenge) == ("BUY: momentum", "earnings risk")
    assert sent.signal == rig.held.signal
    assert sent.hitl_override.operator_id == "alice"
    versions = dict(sent.model_versions)
    assert versions["snapshot-source"] == "retained-debate-context"
    assert versions["retained-context-audit-seq"] == "7"
    assert versions["cognitive-core/judge"] == "judge-model"


def test_without_a_retained_record_the_fallback_is_labelled(pb: dict[str, Any]) -> None:
    rig = Rig(pb, FakeRecords())
    status, hold = rig.decide()
    assert status == 200 and hold["execution"]["snapshotSource"] == "hold-signal-only"
    versions = dict(rig.relay.requests[0].context.model_versions)
    assert versions["snapshot-source"] == "hold-signal-only"
    assert versions["retained-context"] == "absent"


def test_identical_duplicates_are_fine(pb: dict[str, Any]) -> None:
    held = make_held(pb)
    context = debate_context(pb, held)
    records = [record_for(held, context, seq=3), record_for(held, context, seq=9)]
    rig = Rig(pb, FakeRecords(records))
    assert rig.decide()[1]["execution"]["snapshotSource"] == "retained-debate-context"
    assert dict(rig.relay.requests[0].context.model_versions)["retained-context-audit-seq"] == "3"


def _refused(pb: dict[str, Any], source: FakeRecords, expected: tuple[int, str]) -> Rig:
    rig = Rig(pb, source)
    assert code(rig.decide()) == expected
    assert rig.holds.resolved == []  # checked before Aegis is asked to release
    assert rig.relay.requests == []
    return rig


def test_a_record_that_does_not_match_the_hold_refuses_the_release(pb: dict[str, Any]) -> None:
    held = make_held(pb)
    context = debate_context(pb, held)
    mismatch = (409, "retained_context_mismatch")
    _refused(pb, FakeRecords([record_for(held, context, context_sha256="0" * 64)]), mismatch)
    _refused(pb, FakeRecords([record_for(held, context, schema="v0")]), mismatch)
    _refused(pb, FakeRecords([record_for(held, context, context_proto_b64="%%%")]), mismatch)
    other_signal = debate_context(pb, held)
    other_signal.signal.quantity_nanos += 1  # not what Aegis held
    _refused(pb, FakeRecords([record_for(held, other_signal)]), mismatch)
    other_decision = make_held(pb)
    other_decision.decision.decided_at_ns += 1
    _refused(pb, FakeRecords([record_for(other_decision, context)]), mismatch)


def test_conflicting_records_refuse_the_release(pb: dict[str, Any]) -> None:
    held = make_held(pb)
    one = debate_context(pb, held)
    two = debate_context(pb, held)
    two.judge_synthesis = "a different story"
    source = FakeRecords([record_for(held, one, seq=3), record_for(held, two, seq=4)])
    _refused(pb, source, (409, "retained_context_conflict"))


def test_a_tampered_or_unreadable_store_refuses_the_release(pb: dict[str, Any]) -> None:
    _refused(pb, FakeRecords(error=Tampered("seq 3")), (409, "retained_context_integrity"))
    rig = _refused(pb, FakeRecords(error=OSError("db down")), (503, "retained_context_unavailable"))
    rig.source.error = None
    assert rig.decide()[0] == 200  # 503 is not cached: a retry decides


def test_a_rejection_does_not_read_the_retained_context(pb: dict[str, Any]) -> None:
    rig = Rig(pb, FakeRecords(error=OSError("db down")))
    assert rig.decide("REJECT")[1]["hitlStatus"] == "REJECTED"
    assert rig.source.queries == []


def test_two_approver_release_loads_it_on_the_second_approval(pb: dict[str, Any]) -> None:
    held = make_held(pb)
    rig = Rig(
        pb,
        FakeRecords([record_for(held, debate_context(pb, held))]),
        **{"quantity_threshold": Decimal(5)},
    )
    assert rig.decide()[0] == 200
    assert rig.source.queries == []  # first of two approvals: nothing to release yet
    bob = Operator(sub="bob", role="approver", amr=("mfa",))
    body = {
        "decision": "APPROVE",
        "reason": "second pair of eyes",
        "clientRequestId": str(uuid.uuid4()),
    }
    status, hold = rig.service.decide(bob, HOLD_ID, body)
    assert status == 200 and hold["execution"]["snapshotSource"] == "retained-debate-context"
