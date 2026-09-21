"""Hash-chain behaviour against a real PostgreSQL: appends, concurrency, verification, tamper
detection, anchors,
fail-closed writes. Requires Docker."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg2
import pytest
from pg_harness import PgInstance, triggers_disabled

from afe_audit import (
    AnchorPublisher,
    AuditEvent,
    AuditLogger,
    AuditValidationError,
    AuditWriteError,
    ChainVerifier,
    DsnConnectionSource,
    FileAnchorSink,
)
from afe_audit.canonical import build_canonical, hash_canonical
from afe_audit.models import Anchor

pytestmark = pytest.mark.integration


def _super(pg: PgInstance, sql: str, params: tuple[object, ...] | None = None) -> None:
    with contextlib.closing(psycopg2.connect(pg.super_dsn)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params)


def _sql_verify(pg: PgInstance, lo: int | None = None, hi: int | None = None) -> tuple[object, ...]:
    with contextlib.closing(psycopg2.connect(pg.app_dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM audit.verify_chain(%s, %s)", (lo, hi))
        row = cur.fetchone()
    assert row is not None
    return row


def _fill(logger: AuditLogger, n: int) -> None:
    for i in range(n):
        logger.record("decision", f"actor-{i % 3}", {"n": i, "note": "é"})


def test_append_builds_a_valid_chain(logger: AuditLogger, verifier: ChainVerifier) -> None:
    first = logger.record("a", "x", {"k": 1})
    second = logger.append(AuditEvent("b", "y", {"k": [1, 2, {"z": None}]}))
    assert (first.seq, second.seq) == (1, 2)
    assert first.prev_hash == "0" * 64
    assert second.prev_hash == first.hash
    assert second.payload == {"k": [1, 2, {"z": None}]}
    result = verifier.verify_chain()
    assert result.ok
    assert (result.rows_checked, result.head_seq, result.head_hash) == (2, 2, second.hash)
    assert verifier.head() == (2, second.hash)


def test_empty_chain_verifies_and_has_no_head(verifier: ChainVerifier) -> None:
    result = verifier.verify_chain()
    assert result.ok
    assert result.rows_checked == 0
    assert verifier.head() is None


def test_concurrent_appends_keep_chain_valid(fresh_pg: PgInstance, verifier: ChainVerifier) -> None:
    threads_n, per_thread = 8, 20
    errors: list[BaseException] = []
    barrier = threading.Barrier(threads_n)

    def work(idx: int) -> None:
        # Generous timeouts: the Docker Desktop port proxy on a loaded dev machine can be slow to
        # accept
        # connections; a timeout would (correctly) fail closed with AuditWriteError and is not what
        # is under test.
        lg = AuditLogger(
            DsnConnectionSource(fresh_pg.app_dsn, connect_timeout_s=60),
            lock_timeout_ms=120_000,
            statement_timeout_ms=120_000,
        )
        barrier.wait()
        try:
            for i in range(per_thread):
                lg.record("concurrent", f"t{idx}", {"i": i})
        except BaseException as exc:  # noqa: BLE001  -- collected and asserted below
            errors.append(exc)

    workers = [threading.Thread(target=work, args=(i,)) for i in range(threads_n)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    assert errors == []
    result = verifier.verify_chain()
    assert result.ok, result.first_break
    assert result.rows_checked == threads_n * per_thread
    with contextlib.closing(psycopg2.connect(fresh_pg.app_dsn)) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), min(seq), max(seq), count(DISTINCT prev_hash) FROM audit.audit_events"
        )
        assert cur.fetchone() == (160, 1, 160, 160)
    assert _sql_verify(fresh_pg)[1] is None  # SQL verification agrees


def _tamper_payload(pg: PgInstance) -> None:
    _super(pg, "UPDATE audit.audit_events SET payload = '{\"n\": 999}'::jsonb WHERE seq = 4")


def _tamper_canonical_only(pg: PgInstance) -> None:
    _super(
        pg,
        "UPDATE audit.audit_events SET canonical = replace(canonical, 'decision', 'decisioX') "
        "WHERE seq = 4",
    )


def _tamper_rehash_row(pg: PgInstance) -> None:
    new = build_canonical(
        4, datetime(2026, 1, 1, tzinfo=UTC), "decision", "evil", {"n": 4}, _prev_hash(pg, 4)
    )
    _super(
        pg,
        "UPDATE audit.audit_events SET canonical=%s, hash=%s, actor='evil', occurred_at=%s, "
        "payload='{\"n\": 4}'::jsonb WHERE seq=4",
        (new, hash_canonical(new), datetime(2026, 1, 1, tzinfo=UTC)),
    )


def _tamper_delete_middle(pg: PgInstance) -> None:
    _super(pg, "DELETE FROM audit.audit_events WHERE seq = 4")


def _tamper_event_type_column(pg: PgInstance) -> None:
    _super(pg, "UPDATE audit.audit_events SET event_type = 'other' WHERE seq = 4")


def _tamper_timestamp_column(pg: PgInstance) -> None:
    _super(
        pg,
        "UPDATE audit.audit_events SET occurred_at = occurred_at + interval '1 second' "
        "WHERE seq = 4",
    )


def _prev_hash(pg: PgInstance, seq: int) -> str:
    with contextlib.closing(psycopg2.connect(pg.app_dsn)) as conn, conn.cursor() as cur:
        cur.execute("SELECT hash FROM audit.audit_events WHERE seq = %s", (seq - 1,))
        row = cur.fetchone()
    assert row is not None
    return str(row[0])


# (tamper, seq at which the FIRST break is reported, defect code) -- Python and SQL must agree.
_TAMPER_CASES: list[tuple[str, Callable[[PgInstance], None], int, str]] = [
    ("payload_only", _tamper_payload, 4, "payload_mismatch"),
    ("canonical_only", _tamper_canonical_only, 4, "hash_mismatch"),
    ("event_type_column", _tamper_event_type_column, 4, "event_type_mismatch"),
    ("timestamp_column", _tamper_timestamp_column, 4, "timestamp_mismatch"),
    ("rehashed_row", _tamper_rehash_row, 5, "prev_hash_link_broken"),
    ("deleted_middle_row", _tamper_delete_middle, 5, "seq_gap"),
]


@pytest.mark.parametrize(
    ("name", "tamper", "seq", "defect"), _TAMPER_CASES, ids=[c[0] for c in _TAMPER_CASES]
)
def test_tampering_is_detected_with_precise_first_break(
    name: str,
    tamper: Callable[[PgInstance], None],
    seq: int,
    defect: str,
    logger: AuditLogger,
    fresh_pg: PgInstance,
    verifier: ChainVerifier,
) -> None:
    _fill(logger, 8)
    assert verifier.verify_chain().ok
    with triggers_disabled(fresh_pg):  # a privileged attacker switching the protections off
        tamper(fresh_pg)
    result = verifier.verify_chain()
    assert not result.ok
    assert result.first_break is not None
    assert (result.first_break.seq, result.first_break.defect) == (seq, defect), name
    sql_row = _sql_verify(fresh_pg)
    assert (sql_row[1], sql_row[2]) == (seq, defect)  # DB-side function agrees with Python


def test_range_verification_semantics(
    logger: AuditLogger, fresh_pg: PgInstance, verifier: ChainVerifier
) -> None:
    _fill(logger, 8)
    with triggers_disabled(fresh_pg):
        _tamper_payload(fresh_pg)  # row 4 only
    assert verifier.verify_chain(1, 3).ok
    broken = verifier.verify_chain(4, 8)
    assert broken.first_break is not None
    assert broken.first_break.seq == 4
    assert verifier.verify_chain(2, 3).ok
    assert verifier.verify_chain(5, 8).ok  # links to row 4's stored hash, which is unchanged
    assert verifier.verify_chain(3, 4).first_break is not None
    sql_row = _sql_verify(fresh_pg, 4, 8)
    assert sql_row[1] == 4


def test_tail_truncation_and_consistent_rewrite_are_caught_only_by_anchors(
    logger: AuditLogger, fresh_pg: PgInstance, verifier: ChainVerifier, tmp_path: object
) -> None:
    from pathlib import Path

    sink = FileAnchorSink(Path(str(tmp_path)) / "anchors.jsonl")
    publisher = AnchorPublisher(verifier, sink)
    assert publisher.publish() is None  # nothing to anchor yet
    _fill(logger, 6)
    anchor = publisher.publish()
    assert anchor is not None
    assert anchor.seq == 6
    assert verifier.verify_anchors(sink.read_all()) == []

    with triggers_disabled(fresh_pg):
        _super(fresh_pg, "DELETE FROM audit.audit_events WHERE seq > 4")
    assert verifier.verify_chain().ok  # the chain alone cannot see tail truncation
    missing = verifier.verify_anchors(sink.read_all())
    assert [(b.seq, b.defect) for b in missing] == [(6, "anchor_row_missing")]

    _fill(logger, 2)  # chain grows again: seq 5, 6 now hold different content
    assert verifier.verify_chain().ok
    rewritten = verifier.verify_anchors(sink.read_all())
    assert [(b.seq, b.defect) for b in rewritten] == [(6, "anchor_hash_mismatch")]


def test_file_anchor_sink_is_itself_tamper_evident(tmp_path: object) -> None:
    from pathlib import Path

    from afe_audit import AnchorError

    path = Path(str(tmp_path)) / "anchors.jsonl"
    sink = FileAnchorSink(path)
    for seq in (1, 2, 3):
        sink.emit(Anchor(seq=seq, hash=f"{seq}" * 64, emitted_at="2026-09-19T00:00:00.000000Z"))
    assert [a.seq for a in sink.read_all()] == [1, 2, 3]
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")  # drop line 2
    with pytest.raises(AnchorError, match="line-hash chain"):
        sink.read_all()
    with pytest.raises(AnchorError):
        sink.emit(Anchor(seq=4, hash="4" * 64, emitted_at="x"))  # refuses to extend a broken file
    path.write_text("not json\n", encoding="utf-8")
    with pytest.raises(AnchorError, match="malformed"):
        sink.read_all()


def test_periodic_publisher_emits_and_stops(
    logger: AuditLogger, verifier: ChainVerifier, tmp_path: object
) -> None:
    from pathlib import Path

    _fill(logger, 2)
    sink = FileAnchorSink(Path(str(tmp_path)) / "a.jsonl")
    publisher = AnchorPublisher(verifier, sink)
    stop = threading.Event()
    assert publisher.run_periodic(0.01, stop, max_iterations=3) == 3
    assert len(sink.read_all()) == 3
    stop.set()
    assert publisher.run_periodic(0.01, stop) == 0
    with pytest.raises(ValueError, match="positive"):
        publisher.run_periodic(0, stop)


def test_write_failure_raises_and_writes_nothing(
    fresh_pg: PgInstance, verifier: ChainVerifier
) -> None:
    dead = AuditLogger(DsnConnectionSource(fresh_pg.dsn("afe_audit_app", "wrong-password")))
    with pytest.raises(AuditWriteError):
        dead.record("t", "a", {})
    unreachable = AuditLogger(
        DsnConnectionSource("host=127.0.0.1 port=1 dbname=x user=x connect_timeout=1")
    )
    with pytest.raises(AuditWriteError):
        unreachable.record("t", "a", {})
    assert verifier.head() is None


def test_lock_timeout_fails_closed_and_leaves_no_row(
    fresh_pg: PgInstance, verifier: ChainVerifier
) -> None:
    blocker = psycopg2.connect(fresh_pg.app_dsn)
    try:
        with blocker.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(audit.chain_lock_key())"
            )  # held until rollback
        impatient = AuditLogger(DsnConnectionSource(fresh_pg.app_dsn), lock_timeout_ms=200)
        with pytest.raises(AuditWriteError, match="lock timeout"):
            impatient.record("t", "a", {})
    finally:
        blocker.rollback()
        blocker.close()
    assert verifier.head() is None
    AuditLogger(DsnConnectionSource(fresh_pg.app_dsn)).record("t", "a", {})  # recovers afterwards


def test_invalid_payload_raises_before_touching_the_database(
    logger: AuditLogger, verifier: ChainVerifier
) -> None:
    with pytest.raises(AuditValidationError):
        logger.record("t", "a", {"price": 1.5})
    with pytest.raises(AuditValidationError):
        AuditEvent("", "a", {})
    assert verifier.head() is None


def test_event_payload_is_frozen_at_construction(logger: AuditLogger) -> None:
    payload: dict[str, object] = {"k": "before"}
    event = AuditEvent("t", "a", payload)
    payload["k"] = "after"
    assert logger.append(event).payload == {"k": "before"}
