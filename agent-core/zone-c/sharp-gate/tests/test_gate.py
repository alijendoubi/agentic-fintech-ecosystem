"""State-machine rules, run against BOTH the in-memory store and the real PostgreSQL-backed
store."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from afe_audit import DsnConnectionSource
from pg_harness import DB_NAME, PgInstance, docker_available, postgres_container
from support import APPROVERS, PROPOSER, FakeAudit, make_gate, make_proposal

from afe_sharp import (
    PIPELINE,
    AuditFailureError,
    ConcurrencyError,
    DistinctApproverError,
    DuplicateProposalError,
    InMemoryProposalStore,
    NotAuthorizedError,
    PostgresProposalStore,
    ProposalClosedError,
    ProposalValidationError,
    SelfApprovalError,
    SharpGate,
    Stage,
    StageSkipError,
    StoreError,
    UnknownProposalError,
)
from afe_sharp.ports import ProposalStore

SQL = Path(__file__).resolve().parents[1] / "sql" / "001_sharp_transitions.sql"
GATES = [s for s in PIPELINE if s is not Stage.DRAFT]


@pytest.fixture(scope="module")
def pg() -> Iterator[PgInstance]:
    if not docker_available():
        pytest.skip("Docker not available")
    with postgres_container() as instance:
        sql = SQL.read_text(encoding="utf-8")
        cmd = ["docker", "exec", "-i", instance.container, "psql", "-v", "ON_ERROR_STOP=1"]
        cmd += ["--no-psqlrc", "-U", "afe_audit_owner", "-d", DB_NAME]
        done = subprocess.run(cmd, input=sql, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"sharp migration failed: {done.stderr}")
        yield instance


@pytest.fixture(params=["memory", pytest.param("postgres", marks=pytest.mark.integration)])
def store(request: pytest.FixtureRequest) -> ProposalStore:
    if request.param == "memory":
        return InMemoryProposalStore()
    pg: PgInstance = request.getfixturevalue("pg")
    return PostgresProposalStore(DsnConnectionSource(pg.app_dsn))


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


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

    gate = G(store, audit, StaticRoleAuthorizer({Stage.COMPLIANCE: [PROPOSER]}))
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
    def __init__(self, inner: ProposalStore) -> None:
        self.inner = inner

    def load(self, proposal_id: str) -> Any:
        return self.inner.load(proposal_id)

    def append(self, event: Any, expected_version: int) -> None:
        raise StoreError("disk full")


def test_store_failure_after_audit_is_recorded_as_aborted(
    store: ProposalStore, audit: FakeAudit, request: pytest.FixtureRequest
) -> None:
    pid = _submit(make_gate(store, audit), request)
    broken = make_gate(_FailingStore(store), audit)
    with pytest.raises(StoreError, match="disk full"):
        broken.approve(pid, "compliance-1", Stage.COMPLIANCE)
    assert audit.events[-1][0] == "sharp.transition_aborted"
    assert audit.events[-1][2]["reason"] == "StoreError"
    assert make_gate(store, audit).get(pid).state is Stage.DRAFT

    class _AbortAuditDown(FakeAudit):
        def record(self, event_type: str, actor: str, payload: Any) -> None:
            if event_type == "sharp.transition_aborted":
                raise RuntimeError("audit down")
            super().record(event_type, actor, payload)

    flaky = make_gate(
        _FailingStore(store), _AbortAuditDown()
    )  # abort-audit failing must not mask the error
    with pytest.raises(StoreError, match="disk full"):
        flaky.approve(pid, "compliance-1", Stage.COMPLIANCE)


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
