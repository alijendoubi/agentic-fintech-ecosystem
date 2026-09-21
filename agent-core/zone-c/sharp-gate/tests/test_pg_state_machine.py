"""Insert-time state machine in PostgreSQL: a compromised holder of the app role (or the owner)
that INSERTs rows directly, bypassing SharpGate, must not be able to store a history that
``fold`` would treat as a promotion."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg2
import pytest
from afe_audit import AuditLogger, DsnConnectionSource
from pg_harness import PgInstance
from test_gate import pg  # noqa: F401  (fixture reuse)

pytestmark = pytest.mark.integration

PROPOSER = "reflector-svc"
REJECTED = "sharp transition rejected"
INVALID = "sharp transition rejected|violates check constraint"  # trigger fires before CHECKs


def _detail(pid: str, proposer: str = PROPOSER) -> str:
    detail = {
        "proposal_id": pid,
        "proposer_id": proposer,
        "description": "d",
        "proposed_change": "c",
        "rationale": "r",
        "evidence_refs": ["e"],
    }
    return json.dumps(detail)


def audit_payload(
    pid: str,
    version: int,
    kind: str,
    frm: str | None,
    to: str,
    occurred_at: datetime,
    detail: str,
) -> dict[str, Any]:
    """The payload SharpGate writes to the audit chain for a transition."""
    return {
        "proposal_id": pid,
        "version": version,
        "kind": kind,
        "from_state": frm,
        "to_state": to,
        "occurred_at": occurred_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "detail": json.loads(detail),
    }


def write_audit(dsn: str, event_type: str, actor: str, payload: dict[str, Any]) -> tuple[int, str]:
    """Append a REAL record to the hash-chained audit table (the app role may, and so may an
    attacker holding it) and return its (seq, hash)."""
    record = AuditLogger(DsnConnectionSource(dsn)).record(event_type, actor, payload)
    return record.seq, record.hash


def raw_insert(
    dsn: str,
    pid: str,
    version: int,
    kind: str,
    frm: str | None,
    to: str,
    actor: str,
    detail: str | None = None,
    *,
    audit_ref: tuple[int, str] | None | Literal["auto"] = "auto",
    occurred_at: datetime | None = None,
) -> None:
    """INSERT one transition directly, bypassing SharpGate (what a compromised service could do).
    ``audit_ref="auto"`` first writes the exact, valid audit record for the row."""
    if detail is None:
        detail = _detail(pid) if kind == "submitted" else json.dumps({"reason": "r"})
    when = occurred_at or datetime.now(UTC)
    if isinstance(audit_ref, str):
        payload = audit_payload(pid, version, kind, frm, to, when, detail)
        audit_ref = write_audit(dsn, f"sharp.{kind}", actor, payload)
    seq, digest = audit_ref if audit_ref else (None, None)
    with contextlib.closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sharp.transitions (proposal_id, version, kind, from_state, to_state, "
            "actor, occurred_at, detail, audit_seq, audit_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
            (pid, version, kind, frm, to, actor, when, detail, seq, digest),
        )
        conn.commit()


def _submitted(dsn: str, pid: str) -> None:
    raw_insert(dsn, pid, 1, "submitted", None, "DRAFT", PROPOSER)


def test_forged_stage_skips_are_refused(pg: PgInstance) -> None:  # noqa: F811
    """The finding: forged approvals with arbitrary actors must not be storable."""
    _submitted(pg.app_dsn, "forge-skip")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # DRAFT -> LEGAL
        raw_insert(pg.app_dsn, "forge-skip", 2, "approved", "DRAFT", "LEGAL", "mallory-1")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # straight to PROMOTED
        raw_insert(pg.app_dsn, "forge-skip", 2, "approved", "DRAFT", "PROMOTED", "mallory-1")


def test_first_row_must_be_a_wellformed_submission_by_the_proposer(pg: PgInstance) -> None:  # noqa: F811
    dsn = pg.app_dsn
    with pytest.raises(psycopg2.Error, match=REJECTED):
        raw_insert(dsn, "f-first", 1, "approved", None, "COMPLIANCE", "mallory-1")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # version must start at 1
        raw_insert(dsn, "f-first", 2, "submitted", None, "DRAFT", PROPOSER)
    with pytest.raises(psycopg2.Error, match=REJECTED):  # must create DRAFT
        raw_insert(dsn, "f-first", 1, "submitted", None, "COMPLIANCE", PROPOSER)
    with pytest.raises(psycopg2.Error, match=REJECTED):  # actor is not the detail's proposer
        raw_insert(dsn, "f-first", 1, "submitted", None, "DRAFT", "someone-else")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # detail names another proposal
        raw_insert(dsn, "f-first", 1, "submitted", None, "DRAFT", PROPOSER, _detail("other"))


@pytest.mark.parametrize(
    ("version", "kind", "frm", "to", "actor"),
    [
        pytest.param(3, "approved", "DRAFT", "COMPLIANCE", "mallory-1", id="version-gap"),
        pytest.param(1, "approved", "DRAFT", "COMPLIANCE", "mallory-1", id="version-replay"),
        pytest.param(2, "approved", "COMPLIANCE", "LEGAL", "mallory-1", id="wrong-from-state"),
        pytest.param(2, "approved", None, "COMPLIANCE", "mallory-1", id="null-from-state"),
        pytest.param(2, "approved", "DRAFT", "COMPLIANCE", PROPOSER, id="self-approval"),
        pytest.param(2, "approved", "DRAFT", "COMPLIANCE", " REFLECTOR-SVC ", id="self-approval-b"),
        pytest.param(2, "submitted", "DRAFT", "DRAFT", "mallory-1", id="second-submitted"),
        pytest.param(2, "rejected", "DRAFT", "COMPLIANCE", "mallory-1", id="reject-to-stage"),
        pytest.param(2, "approved", "DRAFT", "REJECTED", "mallory-1", id="approve-into-rejected"),
    ],
)
def test_forged_rows_violating_an_invariant_are_refused(
    pg: PgInstance,  # noqa: F811
    request: pytest.FixtureRequest,
    version: int,
    kind: str,
    frm: str | None,
    to: str,
    actor: str,
) -> None:
    pid = f"f-inv-{request.node.callspec.id}"
    _submitted(pg.app_dsn, pid)
    with pytest.raises(psycopg2.Error, match=REJECTED):
        raw_insert(pg.app_dsn, pid, version, kind, frm, to, actor)


def test_forged_row_with_invalid_kind_state_or_reason_is_refused(pg: PgInstance) -> None:  # noqa: F811
    dsn = pg.app_dsn
    _submitted(dsn, "f-kind")
    for bad_kind in ("bogus", ""):
        with pytest.raises(psycopg2.Error, match=INVALID):
            raw_insert(dsn, "f-kind", 2, bad_kind, "DRAFT", "COMPLIANCE", "mallory-1")
    with pytest.raises(psycopg2.Error, match=INVALID):
        raw_insert(dsn, "f-kind", 2, "approved", "DRAFT", "NOT_A_STAGE", "mallory-1")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # rejection without a reason
        raw_insert(dsn, "f-kind", 2, "rejected", "DRAFT", "REJECTED", "mallory-1", "{}")


def test_approver_cannot_be_reused_and_closed_proposals_cannot_be_extended(
    pg: PgInstance,  # noqa: F811
) -> None:
    dsn = pg.app_dsn
    _submitted(dsn, "f-2")
    raw_insert(dsn, "f-2", 2, "approved", "DRAFT", "COMPLIANCE", "signer-a")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # same identity, normalised
        raw_insert(dsn, "f-2", 3, "approved", "COMPLIANCE", "LEGAL", " Signer-A")
    raw_insert(dsn, "f-2", 3, "rejected", "COMPLIANCE", "REJECTED", "signer-b")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # nothing after REJECTED
        raw_insert(dsn, "f-2", 4, "approved", "REJECTED", "LEGAL", "signer-c")
    with pytest.raises(psycopg2.Error, match=REJECTED):
        raw_insert(dsn, "f-2", 4, "approved", "COMPLIANCE", "LEGAL", "signer-c")


def test_owner_and_superuser_inserts_are_validated_too(pg: PgInstance) -> None:  # noqa: F811
    with pytest.raises(psycopg2.Error, match=REJECTED):
        raw_insert(pg.owner_dsn, "f-3", 1, "approved", None, "COMPLIANCE", "x")
    with contextlib.closing(psycopg2.connect(pg.super_dsn)) as conn, conn.cursor() as cur:
        cur.execute("SET session_replication_role = replica")  # ENABLE ALWAYS must still fire
        with pytest.raises(psycopg2.Error, match=REJECTED):
            cur.execute(
                "INSERT INTO sharp.transitions (proposal_id, version, kind, from_state, to_state, "
                "actor, occurred_at, detail, audit_seq, audit_hash) VALUES ('f-3', 1, 'approved', "
                "NULL, 'COMPLIANCE', 'x', now(), '{}', 1, repeat('0', 64))"
            )


# -- every transition must reference a matching record of the hash-chained audit log -------------


def test_row_without_a_real_audit_record_is_refused(pg: PgInstance) -> None:  # noqa: F811
    """No audit record, no transition: a forged row cannot exist without leaving a chain entry."""
    dsn = pg.app_dsn
    with pytest.raises(psycopg2.Error, match=REJECTED):  # no reference at all
        raw_insert(dsn, "a-1", 1, "submitted", None, "DRAFT", PROPOSER, audit_ref=None)
    with pytest.raises(psycopg2.Error, match=REJECTED):  # reference to a record that does not exist
        raw_insert(dsn, "a-1", 1, "submitted", None, "DRAFT", PROPOSER,
                   audit_ref=(2_000_000_000, "0" * 64))  # fmt: skip
    _submitted(dsn, "a-1")
    with pytest.raises(psycopg2.Error, match=REJECTED):
        raw_insert(dsn, "a-1", 2, "approved", "DRAFT", "COMPLIANCE", "c-1", audit_ref=None)


def test_row_must_match_the_audit_record_it_references(pg: PgInstance) -> None:  # noqa: F811
    dsn = pg.app_dsn
    when = datetime.now(UTC)
    _submitted(dsn, "a-2")
    detail = json.dumps({"reason": "r"})
    good = audit_payload("a-2", 2, "rejected", "DRAFT", "REJECTED", when, detail)
    seq, digest = write_audit(dsn, "sharp.rejected", "compliance-1", good)

    def insert(**changes: Any) -> None:
        row: dict[str, Any] = {
            "version": 2, "kind": "rejected", "frm": "DRAFT", "to": "REJECTED",
            "actor": "compliance-1", "audit_ref": (seq, digest), "occurred_at": when,
        }  # fmt: skip
        row.update(changes)
        raw_insert(dsn, "a-2", row.pop("version"), row.pop("kind"), row.pop("frm"),
                   row.pop("to"), row.pop("actor"), detail, **row)  # fmt: skip

    with pytest.raises(psycopg2.Error, match=REJECTED):  # different actor than the audit record
        insert(actor="compliance-2")
    with pytest.raises(psycopg2.Error, match=REJECTED):  # right seq, wrong hash
        insert(audit_ref=(seq, "f" * 64))
    with pytest.raises(psycopg2.Error, match=REJECTED):  # different timestamp
        insert(occurred_at=when + timedelta(seconds=5))
    other_proposal = audit_payload("someone-else", 2, "rejected", "DRAFT", "REJECTED", when, detail)
    foreign = write_audit(dsn, "sharp.rejected", "compliance-1", other_proposal)
    with pytest.raises(psycopg2.Error, match=REJECTED):  # another proposal's audit record
        insert(audit_ref=foreign)
    aborted = write_audit(dsn, "sharp.transition_aborted", "compliance-1", good)
    with pytest.raises(psycopg2.Error, match=REJECTED):  # wrong event type (e.g. an abort record)
        insert(audit_ref=aborted)
    insert()  # the exact, matching record is accepted
    with pytest.raises(psycopg2.Error, match=REJECTED):  # and cannot back a second row
        raw_insert(dsn, "a-2", 3, "rejected", "REJECTED", "REJECTED", "compliance-1", detail,
                   audit_ref=(seq, digest), occurred_at=when)  # fmt: skip
