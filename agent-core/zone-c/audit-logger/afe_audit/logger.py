"""AuditLogger: hash-chained, append-only writer.

Fail-closed contract: ``append`` returns an AuditRecord only after the row is committed. On ANY
failure it raises
(AuditValidationError / AuditWriteError) and the caller MUST NOT perform the action being audited.

Concurrency: every append takes the same transaction-scoped advisory lock
(``audit.chain_lock_key()``), reads the
head, computes the next hash and inserts, all in one transaction. The database insert trigger re-
takes the lock and
re-validates the row, so even a misbehaving client cannot fork or gap the chain.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import psycopg2

from afe_audit.canonical import GENESIS_HASH, build_canonical, hash_canonical
from afe_audit.db import ConnectionSource
from afe_audit.errors import AuditWriteError
from afe_audit.models import AuditEvent, AuditRecord

Clock = Callable[[], datetime]
DEFAULT_LOCK_TIMEOUT_MS = 5_000
DEFAULT_STATEMENT_TIMEOUT_MS = 10_000


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AuditLogger:
    def __init__(
        self,
        source: ConnectionSource,
        clock: Clock = _utc_now,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    ) -> None:
        if lock_timeout_ms <= 0 or statement_timeout_ms <= 0:
            raise ValueError("timeouts must be positive")
        self._source = source
        self._clock = clock
        self._lock_timeout_ms = int(lock_timeout_ms)
        self._statement_timeout_ms = int(statement_timeout_ms)

    def record(self, event_type: str, actor: str, payload: Mapping[str, object]) -> AuditRecord:
        """Convenience wrapper used by other Zone C packages (structural ``AuditSink`` protocol)."""
        return self.append(AuditEvent(event_type=event_type, actor=actor, payload=payload))

    def append(self, event: AuditEvent) -> AuditRecord:
        try:
            with self._source.connection() as conn:
                try:
                    record = self._append_in_txn(conn, event)
                    conn.commit()
                except BaseException:
                    _rollback_quietly(conn)
                    raise
                return record
        except psycopg2.Error as exc:
            raise AuditWriteError(f"audit write failed: {exc.__class__.__name__}: {exc}") from exc

    def _append_in_txn(self, conn: Any, event: AuditEvent) -> AuditRecord:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = {self._lock_timeout_ms}")
            cur.execute(f"SET LOCAL statement_timeout = {self._statement_timeout_ms}")
            cur.execute("SELECT pg_advisory_xact_lock(audit.chain_lock_key())")
            cur.execute("SELECT seq, hash FROM audit.audit_events ORDER BY seq DESC LIMIT 1")
            head = cur.fetchone()
            seq, prev_hash = (head[0] + 1, head[1]) if head else (1, GENESIS_HASH)
            occurred_at = self._clock()
            canonical = build_canonical(
                seq, occurred_at, event.event_type, event.actor, event.payload, prev_hash
            )
            digest = hash_canonical(canonical)
            cur.execute(
                "INSERT INTO audit.audit_events "
                "(seq, occurred_at, event_type, actor, payload, canonical, prev_hash, hash) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
                (
                    seq,
                    occurred_at,
                    event.event_type,
                    event.actor,
                    event.payload_json,
                    canonical,
                    prev_hash,
                    digest,
                ),
            )
        return AuditRecord(
            seq=seq,
            occurred_at=occurred_at,
            event_type=event.event_type,
            actor=event.actor,
            prev_hash=prev_hash,
            hash=digest,
            canonical=canonical,
        )


def _rollback_quietly(conn: Any) -> None:
    # If the connection is already broken the original exception is what matters; it is re-raised by
    # the caller.
    with contextlib.suppress(psycopg2.Error):
        conn.rollback()
