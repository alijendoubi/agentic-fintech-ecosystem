"""Independent (client-side) chain verification.

Deliberately re-implemented in Python rather than delegating to ``audit.verify_chain``: if the database owner's
functions were replaced, this check still recomputes every hash from the stored canonical text. Defect codes match
the SQL function; Python additionally reports ``non_canonical_serialization``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg2

from afe_audit.canonical import GENESIS_HASH, canonical_json, format_timestamp, hash_canonical
from afe_audit.db import ConnectionSource
from afe_audit.errors import AuditWriteError
from afe_audit.models import Anchor, ChainBreak, VerificationResult

BATCH_SIZE = 1_000
_COLUMNS = "seq, occurred_at, event_type, actor, payload, canonical, prev_hash, hash"


@dataclass(frozen=True)
class _Row:
    seq: int
    occurred_at: datetime
    event_type: str
    actor: str
    payload: Any
    canonical: str
    prev_hash: str
    hash: str


def row_defect(row: _Row, expected_prev_hash: str) -> tuple[str, str | None, str | None] | None:
    """Return (defect, expected, actual) for the first inconsistency in a row, else None."""
    if row.hash != hash_canonical(row.canonical):
        return "hash_mismatch", hash_canonical(row.canonical), row.hash
    try:
        doc = json.loads(row.canonical)
    except ValueError:
        return "canonical_unparseable", None, None
    if not isinstance(doc, dict):
        return "canonical_unparseable", None, None
    if row.prev_hash != expected_prev_hash:
        return "prev_hash_link_broken", expected_prev_hash, row.prev_hash
    checks: Sequence[tuple[str, Any, Any]] = (
        ("canonical_prev_hash_mismatch", doc.get("prev_hash"), row.prev_hash),
        ("seq_mismatch", doc.get("seq"), row.seq),
        ("event_type_mismatch", doc.get("event_type"), row.event_type),
        ("actor_mismatch", doc.get("actor"), row.actor),
        ("payload_mismatch", doc.get("payload"), row.payload),
        ("timestamp_mismatch", doc.get("occurred_at"), format_timestamp(row.occurred_at)),
    )
    for defect, in_canonical, in_column in checks:
        if in_canonical != in_column:
            return defect, str(in_canonical), str(in_column)
    if _recanonicalise(doc) != row.canonical:
        return "non_canonical_serialization", None, None
    return None


def _recanonicalise(doc: dict[str, Any]) -> str | None:
    try:
        return canonical_json(doc)
    except Exception:  # noqa: BLE001  -- any failure means the stored text is not a valid canonical document
        return None


class ChainVerifier:
    def __init__(self, source: ConnectionSource) -> None:
        self._source = source

    def head(self) -> tuple[int, str] | None:
        """(seq, hash) of the newest row, or None when the table is empty."""
        try:
            with self._source.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT seq, hash FROM audit.audit_events ORDER BY seq DESC LIMIT 1")
                row = cur.fetchone()
        except psycopg2.Error as exc:
            raise AuditWriteError(f"cannot read audit head: {exc}") from exc
        return (int(row[0]), str(row[1])) if row else None

    def verify_chain(self, from_seq: int | None = None, to_seq: int | None = None) -> VerificationResult:
        """Verify all rows, or the inclusive range [from_seq, to_seq]. Returns the FIRST break, if any.

        Range semantics match ``audit.verify_chain``: the row before ``from_seq`` supplies the expected prev_hash;
        with no predecessor the first row must be seq 1 with the genesis prev_hash."""
        try:
            with self._source.connection() as conn:
                pred = self._predecessor(conn, from_seq)
                return self._walk(self._iter_rows(conn, from_seq, to_seq), pred)
        except psycopg2.Error as exc:
            raise AuditWriteError(f"cannot read audit chain: {exc}") from exc

    def verify_anchors(self, anchors: Iterable[Anchor]) -> list[ChainBreak]:
        """Compare externally published anchors with the table. Detects tail truncation and consistent rewrites
        that verify_chain alone cannot see. Returns [] when every anchor matches."""
        problems: list[ChainBreak] = []
        try:
            with self._source.connection() as conn, conn.cursor() as cur:
                for anchor in anchors:
                    cur.execute("SELECT hash FROM audit.audit_events WHERE seq = %s", (anchor.seq,))
                    row = cur.fetchone()
                    if row is None:
                        problems.append(ChainBreak(anchor.seq, "anchor_row_missing", anchor.hash, None))
                    elif row[0] != anchor.hash:
                        problems.append(ChainBreak(anchor.seq, "anchor_hash_mismatch", anchor.hash, str(row[0])))
        except psycopg2.Error as exc:
            raise AuditWriteError(f"cannot verify anchors: {exc}") from exc
        return problems

    @staticmethod
    def _predecessor(conn: Any, from_seq: int | None) -> tuple[int, str]:
        if from_seq is None:
            return 0, GENESIS_HASH
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seq, hash FROM audit.audit_events WHERE seq < %s ORDER BY seq DESC LIMIT 1", (from_seq,)
            )
            row = cur.fetchone()
        return (int(row[0]), str(row[1])) if row else (0, GENESIS_HASH)

    @staticmethod
    def _iter_rows(conn: Any, from_seq: int | None, to_seq: int | None) -> Iterator[_Row]:
        query = (
            f"SELECT {_COLUMNS} FROM audit.audit_events "
            "WHERE (%(lo)s::bigint IS NULL OR seq >= %(lo)s) AND (%(hi)s::bigint IS NULL OR seq <= %(hi)s) "
            "ORDER BY seq"
        )
        with conn.cursor(name="afe_audit_verify") as cur:
            cur.itersize = BATCH_SIZE
            cur.execute(query, {"lo": from_seq, "hi": to_seq})
            for rec in cur:
                yield _Row(*rec)

    @staticmethod
    def _walk(rows: Iterator[_Row], pred: tuple[int, str]) -> VerificationResult:
        p_seq, p_hash = pred
        checked = 0
        for row in rows:
            checked += 1
            if row.seq != p_seq + 1:
                brk = ChainBreak(row.seq, "seq_gap", str(p_seq + 1), str(row.seq))
                return VerificationResult(False, checked, brk, p_seq, p_hash)
            found = row_defect(row, p_hash)
            if found is not None:
                brk = ChainBreak(row.seq, *found)
                return VerificationResult(False, checked, brk, p_seq, p_hash)
            p_seq, p_hash = row.seq, row.hash
        return VerificationResult(True, checked, None, p_seq, p_hash)
