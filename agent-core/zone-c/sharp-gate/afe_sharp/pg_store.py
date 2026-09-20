"""PostgreSQL ProposalStore over its OWN append-only table ``sharp.transitions``
(sql/001_sharp_transitions.sql).

Choice (documented): a dedicated event-sourced table rather than reusing ``audit.audit_events``,
because state
must be queried per proposal with optimistic concurrency (UNIQUE(proposal_id, version)); the
hash-chained audit
log receives a copy of every transition through the AuditSink, so tamper-evidence still comes from
the audit
chain. The table uses the same immutability pattern (statement-level triggers that reject
UPDATE/DELETE/TRUNCATE
for every role, INSERT+SELECT only for the runtime role).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

import psycopg2
from psycopg2 import errors as pg_errors

from afe_sharp.errors import ConcurrencyError, StoreError
from afe_sharp.models import Stage, TransitionEvent

_COLUMNS = "proposal_id, version, kind, from_state, to_state, actor, occurred_at, detail::text"


class ConnectionSource(Protocol):
    def connection(self) -> Any:  # context manager yielding a psycopg2 connection
        ...


class PostgresProposalStore:
    def __init__(self, source: ConnectionSource, lock_timeout_ms: int = 5_000) -> None:
        self._source = source
        self._lock_timeout_ms = int(lock_timeout_ms)

    def load(self, proposal_id: str) -> list[TransitionEvent]:
        try:
            with self._source.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    f"SELECT {_COLUMNS} FROM sharp.transitions "
                    "WHERE proposal_id = %s ORDER BY version",
                    (proposal_id,),
                )
                return [_event(row) for row in cur.fetchall()]
        except psycopg2.Error as exc:
            raise StoreError(f"cannot load proposal history: {exc}") from exc

    def append(self, event: TransitionEvent, expected_version: int) -> None:
        if event.version != expected_version + 1:
            raise ConcurrencyError("event version must be expected_version + 1")
        try:
            with self._source.connection() as conn:
                try:
                    self._insert(conn, event, expected_version)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except pg_errors.UniqueViolation as exc:
            raise ConcurrencyError("another transition was recorded first") from exc
        except psycopg2.Error as exc:
            raise StoreError(f"cannot append transition: {exc}") from exc

    def _insert(self, conn: Any, event: TransitionEvent, expected_version: int) -> None:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL lock_timeout = {self._lock_timeout_ms}")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (event.proposal_id,))
            cur.execute(
                "SELECT count(*) FROM sharp.transitions WHERE proposal_id = %s",
                (event.proposal_id,),
            )
            row = cur.fetchone()
            if row is None or int(row[0]) != expected_version:
                raise ConcurrencyError(f"expected version {expected_version}, store has {row}")
            cur.execute(
                "INSERT INTO sharp.transitions (proposal_id, version, kind, from_state, to_state, "
                "actor, occurred_at, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                (
                    event.proposal_id,
                    event.version,
                    event.kind,
                    event.from_state.value if event.from_state else None,
                    event.to_state.value,
                    event.actor,
                    event.occurred_at,
                    event.detail_json,
                ),
            )


def _event(row: tuple[Any, ...]) -> TransitionEvent:
    proposal_id, version, kind, from_state, to_state, actor, occurred_at, detail = row
    assert isinstance(occurred_at, datetime)
    return TransitionEvent(
        proposal_id=proposal_id,
        version=int(version),
        kind=kind,
        from_state=Stage(from_state) if from_state else None,
        to_state=Stage(to_state),
        actor=actor,
        occurred_at=occurred_at,
        detail_json=detail,
    )
