"""Security properties of the real database setup (ALI-49): role separation, append-only for EVERY
role
(including the table owner), DDL lock, and fail-closed initialisation. Requires Docker."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import psycopg2
import pytest
from pg_harness import PgInstance, run_failing_init

from afe_audit import AuditLogger
from afe_audit.canonical import GENESIS_HASH, build_canonical, hash_canonical

pytestmark = pytest.mark.integration

_REPLACE_MUTATION_FN = (
    "CREATE OR REPLACE FUNCTION audit.reject_mutation() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
)
_REPLACE_ROW_DEFECT_FN = (
    "CREATE OR REPLACE FUNCTION audit.row_defect(rec audit.audit_events, expected_prev_hash text) "
    "RETURNS text LANGUAGE sql AS $$ SELECT NULL::text $$"
)

_INSERT = (
    "INSERT INTO audit.audit_events "
    "(seq, occurred_at, event_type, actor, payload, canonical, prev_hash, hash) "
    "VALUES (%s, %s, 'forged', 'mallory', '{}'::jsonb, %s, %s, %s)"
)


def _run(dsn: str, sql: str, params: tuple[object, ...] | None = None) -> None:
    with contextlib.closing(psycopg2.connect(dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _rejected(dsn: str, sql: str) -> str:
    with pytest.raises(psycopg2.Error) as info:
        _run(dsn, sql)
    return str(info.value)


@pytest.fixture
def seeded(logger: AuditLogger) -> AuditLogger:
    for i in range(3):
        logger.record("seed", "system", {"i": i})
    return logger


def test_roles_are_not_superusers_and_app_is_not_owner(fresh_pg: PgInstance) -> None:
    sql = (
        "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
        "WHERE rolname LIKE 'afe_audit_%'"
    )
    with contextlib.closing(psycopg2.connect(fresh_pg.app_dsn)) as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = {r[0]: r[1:] for r in cur.fetchall()}
        assert rows == {
            "afe_audit_owner": (False, False, False),
            "afe_audit_app": (False, False, False),
        }
        cur.execute("SELECT tableowner FROM pg_tables WHERE tablename = 'audit_events'")
        assert cur.fetchone() == ("afe_audit_owner",)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit.audit_events SET actor = 'x'",
        "UPDATE audit.audit_events SET actor = 'x' WHERE false",
        "DELETE FROM audit.audit_events",
        "TRUNCATE audit.audit_events",
    ],
)
def test_app_role_cannot_update_delete_truncate(
    seeded: AuditLogger, fresh_pg: PgInstance, sql: str
) -> None:
    assert "permission denied" in _rejected(fresh_pg.app_dsn, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit.audit_events SET actor = 'x'",
        "UPDATE audit.audit_events SET actor = 'x' WHERE false",
        "DELETE FROM audit.audit_events",
        "DELETE FROM audit.audit_events WHERE false",
        "TRUNCATE audit.audit_events",
    ],
)
def test_table_owner_cannot_update_delete_truncate(
    seeded: AuditLogger, fresh_pg: PgInstance, sql: str
) -> None:
    assert "append-only" in _rejected(fresh_pg.owner_dsn, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit.audit_events SET actor = 'x'",
        "DELETE FROM audit.audit_events",
        "TRUNCATE audit.audit_events",
    ],
)
def test_superuser_cannot_bypass_via_replication_role(
    seeded: AuditLogger, fresh_pg: PgInstance, sql: str
) -> None:
    with pytest.raises(psycopg2.Error, match="append-only"):
        _run(fresh_pg.super_dsn, f"SET session_replication_role = replica; {sql}")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE audit.evil (x int)",
        "CREATE TABLE public.evil (x int)",
        "CREATE TEMP TABLE evil (x int)",
        "CREATE SCHEMA evil",
        "ALTER TABLE audit.audit_events ADD COLUMN evil int",
        "ALTER TABLE audit.audit_events DISABLE TRIGGER USER",
        "DROP TABLE audit.audit_events",
        "DROP TRIGGER audit_events_no_update ON audit.audit_events",
        _REPLACE_MUTATION_FN,
        "CREATE ROLE evil",
    ],
)
def test_app_role_cannot_run_ddl(fresh_pg: PgInstance, sql: str) -> None:
    with pytest.raises(psycopg2.Error, match=r"permission denied|must be owner"):
        _run(fresh_pg.app_dsn, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE audit.audit_events DISABLE TRIGGER audit_events_no_update",
        "ALTER TABLE audit.audit_events DISABLE TRIGGER USER",
        "ALTER TABLE audit.audit_events ADD COLUMN evil int",
        "ALTER TABLE audit.audit_events RENAME TO evil",
        "DROP TRIGGER audit_events_no_delete ON audit.audit_events",
        "DROP TABLE audit.audit_events CASCADE",
        "DROP SCHEMA audit CASCADE",
        _REPLACE_MUTATION_FN,
        _REPLACE_ROW_DEFECT_FN,
    ],
)
def test_ddl_guard_stops_owner_from_weakening_the_table(
    seeded: AuditLogger, fresh_pg: PgInstance, sql: str
) -> None:
    with pytest.raises(psycopg2.Error, match="DDL-locked"):
        _run(fresh_pg.owner_dsn, sql)
    # protections still intact afterwards
    assert "append-only" in _rejected(fresh_pg.owner_dsn, "DELETE FROM audit.audit_events")


def test_chain_insert_trigger_is_enable_always(fresh_pg: PgInstance) -> None:
    sql = (
        "SELECT tgname, tgenabled FROM pg_trigger "
        "WHERE tgrelid = 'audit.audit_events'::regclass AND NOT tgisinternal"
    )
    with contextlib.closing(psycopg2.connect(fresh_pg.app_dsn)) as conn, conn.cursor() as cur:
        cur.execute(sql)
        states = dict(cur.fetchall())
    assert "audit_events_chain_insert" in states
    assert set(states.values()) == {"A"}


def test_chain_validation_cannot_be_bypassed_via_replication_role(
    seeded: AuditLogger, fresh_pg: PgInstance
) -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    fake_prev = "f" * 64  # unique, so only the chain trigger (not a UNIQUE constraint) can refuse it
    forged = build_canonical(9, now, "forged", "mallory", {}, fake_prev)
    params = (9, now, forged, fake_prev, hash_canonical(forged))
    with pytest.raises(psycopg2.Error, match="audit chain"):
        _run(fresh_pg.super_dsn, "SET session_replication_role = replica; " + _INSERT, params)


def test_app_role_cannot_insert_forged_rows(seeded: AuditLogger, fresh_pg: PgInstance) -> None:
    now = datetime(2026, 9, 19, tzinfo=UTC)
    good = build_canonical(4, now, "forged", "mallory", {}, GENESIS_HASH)
    wrong_prev = (4, now, good, GENESIS_HASH, hash_canonical(good))
    with pytest.raises(
        psycopg2.Error, match="chain"
    ):  # right seq, but prev_hash is not the head hash
        _run(fresh_pg.app_dsn, _INSERT, wrong_prev)
    with pytest.raises(psycopg2.Error, match="expected seq 4"):  # gap
        _run(fresh_pg.app_dsn, _INSERT, (9, now, good, GENESIS_HASH, hash_canonical(good)))
    head = seeded.record("more", "system", {})
    forged = build_canonical(head.seq + 1, now, "forged", "mallory", {}, head.hash)
    with pytest.raises(psycopg2.Error, match="hash_mismatch"):  # canonical altered after hashing
        _run(fresh_pg.app_dsn, _INSERT, (head.seq + 1, now, forged, head.hash, "a" * 64))


def test_init_procedure_fails_closed_without_passwords() -> None:
    result = run_failing_init({"AFE_AUDIT_APP_PASSWORD": None})
    assert result.returncode != 0
    assert "AFE_AUDIT_APP_PASSWORD is required" in result.stdout + result.stderr


def test_init_procedure_rejects_weak_or_identical_passwords() -> None:
    same = "s" * 20
    result = run_failing_init({"AFE_AUDIT_OWNER_PASSWORD": same, "AFE_AUDIT_APP_PASSWORD": same})
    assert result.returncode != 0
    assert "division by zero" in result.stdout + result.stderr
