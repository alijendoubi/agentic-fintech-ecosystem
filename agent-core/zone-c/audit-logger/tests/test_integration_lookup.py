"""RecordLookup against a real PostgreSQL: finds records by payload key and refuses tampered ones.
Requires Docker."""

from __future__ import annotations

import contextlib

import psycopg2
import pytest
from pg_harness import PgInstance, triggers_disabled

from afe_audit import (
    AuditIntegrityError,
    AuditLogger,
    AuditWriteError,
    DsnConnectionSource,
    RecordLookup,
)

pytestmark = pytest.mark.integration


def _lookup(pg: PgInstance) -> RecordLookup:
    return RecordLookup(DsnConnectionSource(pg.app_dsn))


def _super(pg: PgInstance, sql: str, params: tuple[object, ...]) -> None:
    with contextlib.closing(psycopg2.connect(pg.super_dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)


def test_find_returns_matching_records_oldest_first(
    logger: AuditLogger, fresh_pg: PgInstance
) -> None:
    logger.record("other", "x", {"hold_id": "h-1"})
    first = logger.record("hold.context.retained", "motor", {"hold_id": "h-1", "n": 1})
    logger.record("hold.context.retained", "motor", {"hold_id": "h-2", "n": 2})
    second = logger.record("hold.context.retained", "motor", {"hold_id": "h-1", "n": 3})
    found = _lookup(fresh_pg).find("hold.context.retained", "hold_id", "h-1")
    assert [r.seq for r in found] == [first.seq, second.seq]
    assert found[0].payload == {"hold_id": "h-1", "n": 1}
    assert found[0].hash == first.hash
    assert _lookup(fresh_pg).find("hold.context.retained", "hold_id", "h-1", limit=1)[0].seq == 2
    assert _lookup(fresh_pg).find("hold.context.retained", "hold_id", "nope") == []


def test_a_tampered_record_is_refused_on_read(logger: AuditLogger, fresh_pg: PgInstance) -> None:
    logger.record("a", "x", {"k": 1})
    target = logger.record("hold.context.retained", "motor", {"hold_id": "h-1", "n": 1})
    with triggers_disabled(fresh_pg):  # a privileged attacker switching the protections off
        _super(
            fresh_pg,
            "UPDATE audit.audit_events SET payload = %s::jsonb WHERE seq = %s",
            ('{"hold_id": "h-1", "n": 2}', target.seq),
        )
    with pytest.raises(AuditIntegrityError) as err:
        _lookup(fresh_pg).find("hold.context.retained", "hold_id", "h-1")
    assert err.value.seq == target.seq
    assert err.value.defect == "payload_mismatch"


def test_a_consistently_rewritten_record_breaks_its_link(
    logger: AuditLogger, fresh_pg: PgInstance
) -> None:
    logger.record("a", "x", {"k": 1})
    target = logger.record("hold.context.retained", "motor", {"hold_id": "h-1"})
    with triggers_disabled(fresh_pg):  # rewrite the predecessor: the target's link no longer holds
        _super(fresh_pg, "UPDATE audit.audit_events SET hash = %s WHERE seq = 1", ("f" * 64,))
    with pytest.raises(AuditIntegrityError) as err:
        _lookup(fresh_pg).find("hold.context.retained", "hold_id", "h-1")
    assert err.value.seq == target.seq
    assert err.value.defect == "prev_hash_link_broken"


def test_an_unreachable_database_is_an_audit_error() -> None:
    lookup = RecordLookup(DsnConnectionSource("host=127.0.0.1 port=1 dbname=x user=x"))
    with pytest.raises(AuditWriteError):
        lookup.find("e", "k", "v")


def test_limits_are_validated() -> None:
    lookup = RecordLookup(DsnConnectionSource("host=127.0.0.1 port=1 dbname=x user=x"))
    with pytest.raises(ValueError):
        lookup.find("e", "k", "v", limit=0)
    with pytest.raises(ValueError):
        RecordLookup(DsnConnectionSource("host=x"), statement_timeout_ms=0)
