"""State-machine rules, run against BOTH the in-memory store and the real PostgreSQL-backed
store."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from afe_audit import AuditLogger, DsnConnectionSource
from pg_harness import DB_NAME, PgInstance, docker_available, postgres_container
from support import APPROVERS, PROPOSER, FakeAudit, make_gate, make_proposal

from afe_sharp import (
    PIPELINE,
    AbortNotRecordedError,
    AuditFailureError,
    ConcurrencyError,
    DistinctApproverError,
    DuplicateProposalError,
    InMemoryProposalStore,
    NotAuthorizedError,
    PostgresAuditLookup,
    PostgresProposalStore,
    ProposalClosedError,
    ProposalValidationError,
    SelfApprovalError,
    SharpGate,
    Stage,
    StageSkipError,
    StoreError,
    TransitionEvent,
    UnknownProposalError,
)
from afe_sharp.models import transition_payload
from afe_sharp.ports import ProposalStore

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
GATES = [s for s in PIPELINE if s is not Stage.DRAFT]


def _psql(container: str, user: str, sql: str) -> None:
    cmd = ["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "--no-psqlrc"]
    cmd += ["-U", user, "-d", DB_NAME]
    done = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"sharp migration failed: {done.stderr}")


@pytest.fixture(scope="module")
def pg() -> Iterator[PgInstance]:
    """Real container initialised by audit-logger's init, then every sharp migration in order:
    NNN_*.sql as afe_audit_owner, the 9NN_* DDL-guard scripts as the superuser."""
    if not docker_available():
        pytest.skip("Docker not available")
    with postgres_container() as instance:
        for path in sorted(SQL_DIR.glob("*.sql")):
            user = "postgres" if path.name.startswith("9") else "afe_audit_owner"
            _psql(instance.container, user, path.read_text(encoding="utf-8"))
        yield instance


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def store(request: pytest.FixtureRequest) -> ProposalStore:
    if request.param == "memory":
        return InMemoryProposalStore()
    pg: PgInstance = request.getfixturevalue("pg")
    return PostgresProposalStore(DsnConnectionSource(pg.app_dsn))


@pytest.fixture
def audit(store: ProposalStore, request: pytest.FixtureRequest) -> FakeAudit:
    """In-memory audit for the memory store; the REAL hash-chained audit table for Postgres, since
    the insert trigger requires every transition to reference an existing audit record."""
    if not isinstance(store, PostgresProposalStore):
        return FakeAudit()
    pg: PgInstance = request.getfixturevalue("pg")
    source = DsnConnectionSource(pg.app_dsn)
    return FakeAudit(backend=AuditLogger(source), lookup=PostgresAuditLookup(source))


@pytest.fixture
def gate(store: ProposalStore, audit: FakeAudit) -> SharpGate:
    return make_gate(store, audit)


def _pid(request: pytest.FixtureRequest) -> str:
    name: str = request.node.name
    return "p-" + name.replace("[", "-").replace("]", "").replace(" ", "")


def _submit(gate: SharpGate, request: pytest.FixtureRequest) -> str:
    pid = _pid(request)
    gate.submit(make_proposal(pid))
    return pid


def _advance(gate: SharpGate, pid: str, upto: Stage) -> None:
    for stage in GATES:
        if PIPELINE.index(stage) > PIPELINE.index(upto):
            return
        gate.approve(pid, APPROVERS[stage], stage)


def test_full_promotion_path_and_audit_trail(
    gate: SharpGate, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    pid = _submit(gate, request)
    assert gate.get(pid).state is Stage.DRAFT
    for stage in GATES:
        rec = gate.approve(pid, APPROVERS[stage], stage, evidence_refs=[f"ev:{stage}"], note="ok")
        assert rec.state is stage
    assert rec.state is Stage.PROMOTED
    assert rec.is_terminal
    assert [a.stage for a in rec.approvals] == GATES
    assert [a.approver_id for a in rec.approvals] == [APPROVERS[s] for s in GATES]
    assert rec.approvals[0].evidence_refs == ("ev:COMPLIANCE",)
    assert rec.version == 7
    kinds = [e[0] for e in audit.events]
    assert kinds == ["sharp.submitted"] + ["sharp.approved"] * 6
    assert audit.events[0][1] == PROPOSER
    assert audit.events[-1][2]["to_state"] == "PROMOTED"


@pytest.mark.parametrize("skip_to", [Stage.LEGAL, Stage.BACKTEST, Stage.CANARY, Stage.PROMOTED])
def test_stages_cannot_be_skipped(
    gate: SharpGate, request: pytest.FixtureRequest, skip_to: Stage
) -> None:
    pid = _submit(gate, request)
    with pytest.raises(StageSkipError):
        gate.approve(pid, APPROVERS[skip_to], skip_to)
    assert gate.get(pid).state is Stage.DRAFT


def test_a_stage_cannot_be_approved_twice_or_replayed(
    gate: SharpGate, request: pytest.FixtureRequest
) -> None:
    pid = _submit(gate, request)
    gate.approve(pid, "compliance-1", Stage.COMPLIANCE)
    with pytest.raises(StageSkipError):
        gate.approve(pid, "compliance-2", Stage.COMPLIANCE)
    with pytest.raises(StageSkipError):
        gate.approve(pid, "compliance-2", Stage.DRAFT)


def test_proposer_cannot_approve_even_with_authorization_or_case_tricks(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    from afe_sharp import SharpGate as G
    from afe_sharp import StaticRoleAuthorizer

    gate = G(
        store, audit, StaticRoleAuthorizer({Stage.COMPLIANCE: [PROPOSER]}), audit_lookup=audit
    )
    pid = _pid(request)
    gate.submit(make_proposal(pid))
    for variant in (PROPOSER, PROPOSER.upper(), f"  {PROPOSER} "):
        with pytest.raises(SelfApprovalError):
            gate.approve(pid, variant, Stage.COMPLIANCE)
    assert gate.get(pid).state is Stage.DRAFT


def test_same_identity_cannot_sign_two_stages(
    gate: SharpGate, request: pytest.FixtureRequest
) -> None:
    pid = _submit(gate, request)
    gate.approve(pid, "compliance-1", Stage.COMPLIANCE)
    gate.approve(pid, "legal-1", Stage.LEGAL)
    gate.approve(pid, "backtest-ci", Stage.BACKTEST)
    gate.approve(pid, "risk-1", Stage.RISK)
    with pytest.raises(DistinctApproverError):
        gate.approve(pid, " RISK-1", Stage.CANARY)  # authorized for CANARY, but already signed RISK
    assert gate.get(pid).state is Stage.RISK


def test_unauthorized_identity_is_refused(gate: SharpGate, request: pytest.FixtureRequest) -> None:
    pid = _submit(gate, request)
    with pytest.raises(NotAuthorizedError):
        gate.approve(pid, "legal-1", Stage.COMPLIANCE)
    with pytest.raises(NotAuthorizedError):
        gate.approve(pid, "nobody", Stage.COMPLIANCE)
    with pytest.raises(ProposalValidationError):
        gate.approve(pid, "  ", Stage.COMPLIANCE)


@pytest.mark.parametrize("at", [Stage.DRAFT, Stage.COMPLIANCE, Stage.BACKTEST, Stage.CANARY])
def test_rejection_at_any_point_is_final(
    gate: SharpGate, audit: FakeAudit, request: pytest.FixtureRequest, at: Stage
) -> None:
    pid = _submit(gate, request)
    _advance(gate, pid, at)
    nxt = gate.get(pid).next_stage
    assert nxt is not None
    rejecter = APPROVERS[nxt]
    rec = gate.reject(pid, rejecter, "not safe")
    assert rec.state is Stage.REJECTED
    assert (rec.rejected_by, rec.rejection_reason) == (rejecter, "not safe")
    assert audit.events[-1][0] == "sharp.rejected"
    assert audit.events[-1][2]["detail"] == {"reason": "not safe"}
    with pytest.raises(ProposalClosedError):
        gate.approve(pid, APPROVERS[nxt], nxt)
    with pytest.raises(ProposalClosedError):
        gate.reject(pid, rejecter, "again")
    assert gate.get(pid).state is Stage.REJECTED


def test_rejection_needs_reason_and_authority(
    gate: SharpGate, request: pytest.FixtureRequest
) -> None:
    pid = _submit(gate, request)
    with pytest.raises(ProposalValidationError):
        gate.reject(pid, "compliance-1", " ")
    with pytest.raises(NotAuthorizedError):
        gate.reject(pid, "legal-1", "wrong stage authority")
    assert gate.get(pid).state is Stage.DRAFT


def test_no_change_after_promotion(gate: SharpGate, request: pytest.FixtureRequest) -> None:
    pid = _submit(gate, request)
    _advance(gate, pid, Stage.PROMOTED)
    with pytest.raises(ProposalClosedError):
        gate.reject(pid, "release-1", "too late")
    with pytest.raises(ProposalClosedError):
        gate.approve(pid, "release-1", Stage.PROMOTED)


def test_duplicate_and_unknown_proposals(gate: SharpGate, request: pytest.FixtureRequest) -> None:
    pid = _submit(gate, request)
    with pytest.raises(DuplicateProposalError):
        gate.submit(make_proposal(pid))
    with pytest.raises(UnknownProposalError):
        gate.get("nope-" + pid)
    with pytest.raises(UnknownProposalError):
        gate.approve("nope-" + pid, "compliance-1", Stage.COMPLIANCE)


def test_audit_failure_blocks_the_transition(
    gate: SharpGate, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    pid = _submit(gate, request)
    audit.fail = True
    with pytest.raises(AuditFailureError):
        gate.approve(pid, "compliance-1", Stage.COMPLIANCE)
    with pytest.raises(AuditFailureError):
        gate.submit(make_proposal(pid + "-new"))
    audit.fail = False
    assert gate.get(pid).state is Stage.DRAFT
    with pytest.raises(UnknownProposalError):
        gate.get(pid + "-new")


def test_concurrent_approvals_yield_exactly_one_winner(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    gate = make_gate(store, audit)
    pid = _submit(gate, request)
    outcomes: list[Any] = []
    barrier = threading.Barrier(2)

    def approve(who: str) -> None:
        barrier.wait()
        try:
            outcomes.append(gate.approve(pid, who, Stage.COMPLIANCE))
        except (ConcurrencyError, StageSkipError) as exc:
            outcomes.append(exc)

    threads = [
        threading.Thread(target=approve, args=(w,)) for w in ("compliance-1", "compliance-2")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [o for o in outcomes if not isinstance(o, Exception)]
    assert len(winners) == 1
    rec = gate.get(pid)
    assert rec.version == 2
    assert len(rec.approvals) == 1


class _FailingStore:
    """Wraps a store whose ``append`` fails with ``error``. ``lands_first`` simulates a commit whose
    acknowledgement is lost (the row IS stored, then the call raises); ``load_fails`` makes the
    follow-up read fail too."""

    def __init__(
        self,
        inner: ProposalStore,
        error: Exception | None = None,
        lands_first: bool = False,
        load_fails: bool = False,
    ) -> None:
        self.inner = inner
        self.error = error or StoreError("disk full")
        self.lands_first = lands_first
        self.load_fails = False
        self._load_fails_after_append = load_fails

    def load(self, proposal_id: str) -> Any:
        if self.load_fails:
            raise StoreError("read failed too")
        return self.inner.load(proposal_id)

    def append(self, event: Any, expected_version: int) -> None:
        if self.lands_first:
            self.inner.append(event, expected_version)
        self.load_fails = self._load_fails_after_append
        raise self.error


class _AbortAuditDown(FakeAudit):
    def record(self, event_type: str, actor: str, payload: Any) -> Any:
        if event_type == "sharp.transition_aborted":
            raise RuntimeError("audit down")
        return super().record(event_type, actor, payload)


def _aborts(audit: FakeAudit) -> list[Any]:
    return [e for e in audit.events if e[0] == "sharp.transition_aborted"]


def test_store_failure_after_audit_is_recorded_as_aborted(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    pid = _submit(make_gate(store, audit), request)
    broken = make_gate(_FailingStore(store), audit)
    with pytest.raises(StoreError, match="disk full"):
        broken.approve(pid, "compliance-1", Stage.COMPLIANCE)
    abort = _aborts(audit)[-1]
    assert audit.events[-1] is abort
    assert abort[2]["reason"] == "StoreError"
    assert abort[2]["store_outcome"] == "absent"
    orphan = audit.events[-2]  # the transition record written before the failed append
    assert orphan[0] == "sharp.approved"
    assert abort[2]["audit_seq"] is not None
    assert abort[2]["proposal_id"] == pid
    assert make_gate(store, audit).get(pid).state is Stage.DRAFT


def test_any_store_failure_not_only_sharp_errors_gets_a_compensating_record(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    pid = _submit(make_gate(store, audit), request)
    for failure in (RuntimeError("boom"), OSError("socket closed"), ValueError("bad")):
        before = len(_aborts(audit))
        broken = make_gate(_FailingStore(store, error=failure), audit)
        with pytest.raises(type(failure)):  # the original error, unchanged
            broken.approve(pid, "compliance-1", Stage.COMPLIANCE)
        assert len(_aborts(audit)) == before + 1
        assert _aborts(audit)[-1][2]["reason"] == type(failure).__name__
    assert make_gate(store, audit).get(pid).state is Stage.DRAFT


def test_a_failed_compensating_record_is_raised_never_swallowed(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    pid = _submit(make_gate(store, audit), request)
    down = _AbortAuditDown(backend=audit.backend, lookup=audit.lookup, entries=audit.entries)
    flaky = make_gate(_FailingStore(store), down)
    with pytest.raises(AbortNotRecordedError, match="disk full") as info:
        flaky.approve(pid, "compliance-1", Stage.COMPLIANCE)
    assert isinstance(info.value, AuditFailureError)  # still a fail-closed audit error
    assert isinstance(info.value.__cause__, StoreError)  # the original failure is preserved
    assert "audit down" in str(info.value)
    assert info.value.audit_seq is not None  # operators can find the orphaned record
    assert make_gate(store, audit).get(pid).state is Stage.DRAFT


def test_a_commit_whose_ack_was_lost_is_not_reported_as_aborted(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    """The row landed but the call raised: claiming 'aborted' would be a lie in the audit log."""
    pid = _submit(make_gate(store, audit), request)
    lossy = make_gate(_FailingStore(store, error=OSError("ack lost"), lands_first=True), audit)
    record = lossy.approve(pid, "compliance-1", Stage.COMPLIANCE)
    assert record.state is Stage.COMPLIANCE
    assert _aborts(audit) == []


def test_unverifiable_outcome_is_flagged_in_the_compensating_record(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    pid = _submit(make_gate(store, audit), request)
    failing = _FailingStore(store, error=OSError("gone"), load_fails=True)
    with pytest.raises(OSError, match="gone"):
        make_gate(failing, audit).approve(pid, "compliance-1", Stage.COMPLIANCE)
    assert _aborts(audit)[-1][2]["store_outcome"] == "unverified"


def test_audit_sink_without_a_record_reference_blocks_the_transition(
    store: ProposalStore, request: pytest.FixtureRequest
) -> None:
    class _NoReceipt(FakeAudit):
        def record(self, event_type: str, actor: str, payload: Any) -> Any:
            super().record(event_type, actor, payload)  # committed, but says nothing about it

    gate = make_gate(store, _NoReceipt())
    with pytest.raises(AuditFailureError, match="record reference"):
        gate.submit(make_proposal(_pid(request)))
    assert store.load(_pid(request)) == []


def test_history_is_immutable_and_records_are_frozen(
    gate: SharpGate, store: ProposalStore, request: pytest.FixtureRequest
) -> None:
    pid = _submit(gate, request)
    rec = gate.approve(pid, "compliance-1", Stage.COMPLIANCE)
    before = store.load(pid)
    with pytest.raises(AttributeError):
        rec.state = Stage.PROMOTED  # type: ignore[misc]
    with pytest.raises(AttributeError):
        rec.approvals[0].approver_id = "x"  # type: ignore[misc]
    assert store.load(pid) == before


def _forge(pid: str, actors: list[str], audit: FakeAudit | None) -> InMemoryProposalStore:
    """A history written around the gate. With ``audit`` each row also gets a real audit record."""
    store = InMemoryProposalStore()
    when = datetime(2026, 9, 19, tzinfo=UTC)
    detail = json.dumps(make_proposal(pid).to_detail())
    rows: list[tuple[str, Stage | None, Stage, str, str]] = [
        ("submitted", None, Stage.DRAFT, PROPOSER, detail)
    ]
    for stage, actor in zip(GATES, actors, strict=True):
        rows.append(("approved", PIPELINE[PIPELINE.index(stage) - 1], stage, actor, "{}"))
    for version, (kind, frm, to, actor, det) in enumerate(rows, start=1):
        event = TransitionEvent(pid, version, kind, frm, to, actor, when, det)
        if audit is not None:
            receipt = audit.record(f"sharp.{kind}", actor, transition_payload(event))
            event = replace(event, audit_seq=receipt.seq, audit_hash=receipt.hash)
        store.append(event, version - 1)
    return store


def test_get_never_reports_promotion_for_a_forged_history() -> None:
    """The finding: rows written around the gate must never read back as PROMOTED."""
    distinct = ["compliance-1", "legal-1", "backtest-ci", "risk-1", "canary-ci", "release-1"]
    # (1) no audit references at all: the transition rows have no audit record
    audit = FakeAudit()
    gate = make_gate(_forge("forged-1", distinct, None), audit)
    with pytest.raises(StoreError, match="audit reference"):
        gate.get("forged-1")
    with pytest.raises(StoreError):
        gate.approve("forged-1", "release-1", Stage.PROMOTED)
    # (2) references, but the audit trail holds no such records
    referenced = _forge("forged-2", distinct, FakeAudit())
    with pytest.raises(StoreError, match="no audit record"):
        make_gate(referenced, FakeAudit()).get("forged-2")
    # (3) real audit records for every row, but one identity signed every stage
    backed = FakeAudit()
    with pytest.raises(StoreError, match="repeated approver"):
        make_gate(_forge("forged-3", ["mallory"] * 6, backed), backed).get("forged-3")
