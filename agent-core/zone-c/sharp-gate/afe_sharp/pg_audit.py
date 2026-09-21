"""Read side of the hash-chained audit log (``audit.audit_events``, SELECT granted to the runtime
role): lets ``SharpGate.get`` / ``fold`` verify that every stored transition is backed by a real,
matching audit record."""

from __future__ import annotations

import json
from collections.abc import Sequence

import psycopg2

from afe_sharp.errors import StoreError
from afe_sharp.models import AuditEntry
from afe_sharp.pg_store import ConnectionSource


class PostgresAuditLookup:
    def __init__(self, source: ConnectionSource) -> None:
        self._source = source

    def find(self, seqs: Sequence[int]) -> dict[int, AuditEntry]:
        if not seqs:
            return {}
        try:
            with self._source.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT seq, hash, event_type, actor, payload::text "
                    "FROM audit.audit_events WHERE seq = ANY(%s)",
                    (list(seqs),),
                )
                rows = cur.fetchall()
        except psycopg2.Error as exc:
            raise StoreError(f"cannot read audit records: {exc}") from exc
        return {
            int(seq): AuditEntry(int(seq), digest, event_type, actor, json.loads(payload))
            for seq, digest, event_type, actor, payload in rows
        }
