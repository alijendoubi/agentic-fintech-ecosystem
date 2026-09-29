"""Integrity-checked read-back of individual audit records by a payload key.

Used where a service must act on something it (or another service) recorded earlier, e.g.
hitl-backend loading the debate context retained with a hold (``hold.context.retained``). Every
record returned has passed the same per-row checks as ``ChainVerifier`` (hash over the stored
canonical text, canonical text consistent with the columns, re-canonicalisation) AND links to the
stored hash of its predecessor. That makes an edited record tamper-evident on read. It does not
prove the rest of the chain: run ``ChainVerifier`` plus external anchors for that.

Lookup is a sequential scan on ``payload ->> key`` (the audit table has no expression index, and
the DDL guard blocks adding one without the superuser break-glass path). TODO(owner): add an index
through that path if the audit table grows large enough for this to matter.
"""

from __future__ import annotations

from typing import Any

import psycopg2

from afe_audit.canonical import GENESIS_HASH
from afe_audit.db import ConnectionSource
from afe_audit.errors import AuditIntegrityError, AuditWriteError
from afe_audit.models import AuditRecord
from afe_audit.verifier import _COLUMNS, _Row, row_defect

MAX_RESULTS = 16
DEFAULT_STATEMENT_TIMEOUT_MS = 10_000


class RecordLookup:
    """Read-only; works with the ``afe_audit_app`` role (SELECT on ``audit.audit_events``)."""

    def __init__(
        self, source: ConnectionSource, statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    ) -> None:
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        self._source = source
        self._timeout_ms = int(statement_timeout_ms)

    def find(self, event_type: str, key: str, value: str, *, limit: int = 2) -> list[AuditRecord]:
        """Records of ``event_type`` whose payload has ``key`` == ``value`` (as text), oldest first,
        at most ``limit``. Raises ``AuditIntegrityError`` if any of them fails its row or link
        check, ``AuditWriteError`` if the database cannot be read (the name is historical: it is
        the "outcome unknown / store unavailable" error of this package)."""
        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"limit must be 1..{MAX_RESULTS}")
        try:
            with self._source.connection() as conn:
                try:
                    return self._find(conn, event_type, key, value, limit)
                finally:
                    conn.rollback()  # read-only transaction; never leave it open
        except psycopg2.Error as exc:
            raise AuditWriteError(f"cannot read audit records: {exc.__class__.__name__}") from exc

    def _find(
        self, conn: Any, event_type: str, key: str, value: str, limit: int
    ) -> list[AuditRecord]:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {self._timeout_ms}")
            cur.execute(
                f"SELECT {_COLUMNS} FROM audit.audit_events "
                "WHERE event_type = %s AND payload ->> %s = %s ORDER BY seq LIMIT %s",
                (event_type, key, value, limit),
            )
            rows = [_Row(*rec) for rec in cur.fetchall()]
            records = []
            for row in rows:
                expected_prev = GENESIS_HASH
                if row.seq > 1:
                    cur.execute(
                        "SELECT hash FROM audit.audit_events WHERE seq = %s", (row.seq - 1,)
                    )
                    pred = cur.fetchone()
                    if pred is None:
                        raise AuditIntegrityError(row.seq, "predecessor_missing")
                    expected_prev = str(pred[0])
                defect = row_defect(row, expected_prev)
                if defect is not None:
                    raise AuditIntegrityError(row.seq, defect[0])
                records.append(
                    AuditRecord(
                        seq=int(row.seq),
                        occurred_at=row.occurred_at,
                        event_type=row.event_type,
                        actor=row.actor,
                        prev_hash=str(row.prev_hash),
                        hash=str(row.hash),
                        canonical=row.canonical,
                    )
                )
        return records
