"""PostgreSQL-specific properties of the SHARP store, with the REAL AuditLogger + hash chain."""

from __future__ import annotations

import contextlib

import psycopg2
import pytest
from afe_audit import AuditLogger, ChainVerifier, DsnConnectionSource
from pg_harness import PgInstance
from support import ALLOWED, APPROVERS, Ticker, make_proposal
from test_gate import GATES, pg  # noqa: F401  (fixture reuse)

from afe_sharp import (
    PostgresAuditLookup,
    PostgresProposalStore,
    SharpGate,
    Stage,
    StaticRoleAuthorizer,
    StoreError,
)

pytestmark = pytest.mark.integration


def _exec(dsn: str, sql: str) -> None:
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)


def test_promotion_flow_persists_and_lands_in_the_real_audit_chain(pg: PgInstance) -> None:  # noqa: F811
    source = DsnConnectionSource(pg.app_dsn)
    audit = AuditLogger(source)
    lookup = PostgresAuditLookup(source)
    gate = SharpGate(
        PostgresProposalStore(source),
        audit,
        StaticRoleAuthorizer(ALLOWED),
        audit_lookup=lookup,
        clock=Ticker(),
    )
    gate.submit(make_proposal("pg-flow"))
    for stage in GATES:
        gate.approve("pg-flow", APPROVERS[stage], stage)
    # a brand-new gate/store (e.g. after a restart) sees the persisted state
    fresh = SharpGate(
        PostgresProposalStore(source), audit, StaticRoleAuthorizer(ALLOWED), audit_lookup=lookup
    )
    assert fresh.get("pg-flow").state is Stage.PROMOTED
    with psycopg2.connect(pg.app_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload->>'to_state' FROM audit.audit_events "
            "WHERE payload->>'proposal_id' = 'pg-flow' ORDER BY seq"
        )
        rows = cur.fetchall()
    assert [r[1] for r in rows] == ["DRAFT", *[s.value for s in GATES]]
    assert rows[0][0] == "sharp.submitted"
    assert ChainVerifier(source).verify_chain().ok


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE sharp.transitions SET actor = 'x'",
        "UPDATE sharp.transitions SET actor = 'x' WHERE false",
        "DELETE FROM sharp.transitions",
        "TRUNCATE sharp.transitions",
    ],
)
def test_history_cannot_be_rewritten_by_app_owner_or_superuser(pg: PgInstance, sql: str) -> None:  # noqa: F811
    with pytest.raises(psycopg2.Error, match="permission denied"):
        _exec(pg.app_dsn, sql)
    with pytest.raises(psycopg2.Error, match="append-only"):
        _exec(pg.owner_dsn, sql)
    with pytest.raises(psycopg2.Error, match="append-only"):
        _exec(pg.super_dsn, f"SET session_replication_role = replica; {sql}")


def test_app_role_cannot_ddl_the_sharp_schema(pg: PgInstance) -> None:  # noqa: F811
    statements = (
        "DROP TABLE sharp.transitions",
        "ALTER TABLE sharp.transitions DISABLE TRIGGER USER",
        "CREATE TABLE sharp.evil (x int)",
    )
    for sql in statements:
        with pytest.raises(psycopg2.Error, match="permission denied|must be owner"):
            _exec(pg.app_dsn, sql)


_SHARP_TRIGGERS = (
    "sharp_no_update",
    "sharp_no_delete",
    "sharp_no_truncate",
    "sharp_enforce_transition",
    "sharp_enforce_audit_reference",
)


def test_every_sharp_trigger_is_enabled_always(pg: PgInstance) -> None:  # noqa: F811
    with psycopg2.connect(pg.app_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'sharp.transitions'::regclass AND NOT tgisinternal"
        )
        states = dict(cur.fetchall())
    assert states == dict.fromkeys(_SHARP_TRIGGERS, "A")  # 'A' = fires in every replication role


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE sharp.transitions DISABLE TRIGGER sharp_enforce_transition",
        "ALTER TABLE sharp.transitions DISABLE TRIGGER sharp_enforce_audit_reference",
        "ALTER TABLE sharp.transitions DISABLE TRIGGER USER",
        "ALTER TABLE sharp.transitions ENABLE TRIGGER sharp_enforce_transition",
        "ALTER TABLE sharp.transitions DROP CONSTRAINT sharp_transitions_states_check",
        "ALTER TABLE sharp.transitions DROP COLUMN audit_seq",
        "ALTER TABLE sharp.transitions RENAME TO transitions_old",
        "ALTER FUNCTION sharp.enforce_transition() SET search_path = public",
        "CREATE OR REPLACE FUNCTION sharp.enforce_transition() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        "DROP TRIGGER sharp_enforce_transition ON sharp.transitions",
        "DROP FUNCTION sharp.enforce_audit_reference() CASCADE",
        "DROP TABLE sharp.transitions",
        "CREATE RULE sharp_bypass AS ON INSERT TO sharp.transitions DO INSTEAD NOTHING",
        "CREATE TRIGGER sharp_extra BEFORE INSERT ON sharp.transitions "
        "FOR EACH ROW EXECUTE FUNCTION sharp.enforce_transition()",
        "CREATE TABLE sharp.evil (x int)",
        "ALTER SCHEMA sharp RENAME TO sharp_old",
        "DROP SCHEMA sharp CASCADE",
    ],
)
def test_owner_cannot_weaken_the_sharp_schema(pg: PgInstance, sql: str) -> None:  # noqa: F811
    """The DDL guard extends to schema 'sharp': the owner role can no longer disable, replace or
    drop the protections (only a superuser, deliberately, can)."""
    with pytest.raises(psycopg2.Error, match="sharp schema is DDL-locked"):
        _exec(pg.owner_dsn, sql)


def test_unreachable_database_fails_closed() -> None:
    down = DsnConnectionSource("host=127.0.0.1 port=1 dbname=x user=x connect_timeout=1")
    with pytest.raises(StoreError):
        PostgresProposalStore(down).load("anything")
    with pytest.raises(StoreError):
        PostgresAuditLookup(down).find([1])


def test_audit_lookup_returns_the_real_chain_records(pg: PgInstance) -> None:  # noqa: F811
    source = DsnConnectionSource(pg.app_dsn)
    record = AuditLogger(source).record("sharp.probe", "actor-x", {"k": [1, "v"]})
    lookup = PostgresAuditLookup(source)
    found = lookup.find([record.seq, 2_000_000_000])
    assert set(found) == {record.seq}
    entry = found[record.seq]
    assert (entry.hash, entry.event_type, entry.actor) == (record.hash, "sharp.probe", "actor-x")
    assert entry.payload == {"k": [1, "v"]}
    assert lookup.find([]) == {}
