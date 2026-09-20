"""Canonical serialisation and hashing of audit records.

Rules (version 1, part of the hash contract -- changing ANY of them requires a new ``v``):

1. The hashed document is a JSON object with exactly the keys
   ``actor, event_type, occurred_at, payload, prev_hash, seq, v`` (sorted).
2. Object keys must be ``str`` and are sorted by Unicode code point; no insignificant whitespace
   (separators ``,`` and ``:``).
3. Strings are written with every non-ASCII character escaped as ``\\uXXXX`` (astral characters as
surrogate pairs),
   so the canonical text is pure ASCII. **No Unicode normalisation** (NFC/NFKC) is applied: composed
   and decomposed
   forms hash differently by design. NUL (``\\u0000``) and lone surrogates are rejected (PostgreSQL
   jsonb cannot
   store NUL; lone surrogates have no UTF-8 encoding).
4. Numbers: only integers in the signed 64-bit range. ``bool`` is a JSON boolean, never a number.
**Floats are
   rejected**: their textual form is not stable across languages and PostgreSQL jsonb re-renders
   numerics.
   Carry decimals as strings (e.g. ``"0.0123"``) or scaled integers.
5. ``None`` -> ``null``; ``list``/``tuple`` -> arrays (order preserved). Any other type is rejected.
   Nesting depth is limited (also stops cycles) and total size is limited.
6. ``occurred_at`` is UTC formatted ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (fixed 6 fractional digits).
7. The hash is ``sha256(canonical_text.encode("utf-8"))`` as 64 lowercase hex characters; the
genesis
   ``prev_hash`` is 64 zeros.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime

from afe_audit.errors import AuditValidationError

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
MAX_DEPTH = 32
MAX_CANONICAL_BYTES = 1_048_576
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _check_string(value: str) -> str:
    if "\x00" in value:
        raise AuditValidationError("string contains NUL (\\u0000), which jsonb cannot store")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuditValidationError("string is not valid Unicode (lone surrogate)") from exc
    return value


def _normalise(value: object, depth: int) -> object:
    if depth > MAX_DEPTH:
        raise AuditValidationError(
            f"payload nesting depth exceeds {MAX_DEPTH} (or contains a cycle)"
        )
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not INT64_MIN <= value <= INT64_MAX:
            raise AuditValidationError("integer outside the int64 range")
        return value
    if isinstance(value, float):
        raise AuditValidationError(
            "float values are not allowed; encode decimals as strings or scaled ints"
        )
    if isinstance(value, str):
        return _check_string(value)
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditValidationError(f"object key must be str, got {type(key).__name__}")
            out[_check_string(key)] = _normalise(item, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_normalise(item, depth + 1) for item in value]
    raise AuditValidationError(f"unsupported type in payload: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Serialise ``value`` under the canonical rules above."""
    text = json.dumps(
        _normalise(value, 0),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    if len(text) > MAX_CANONICAL_BYTES:
        raise AuditValidationError(f"canonical form exceeds {MAX_CANONICAL_BYTES} bytes")
    return text


def format_timestamp(moment: datetime) -> str:
    """UTC, fixed microsecond precision. Naive datetimes are rejected."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise AuditValidationError("timestamp must be timezone-aware")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_canonical(
    seq: int,
    occurred_at: datetime,
    event_type: str,
    actor: str,
    payload: Mapping[str, object],
    prev_hash: str,
) -> str:
    """Canonical text of one chain record. This exact text is stored and hashed."""
    if seq < 1:
        raise AuditValidationError("seq must be >= 1")
    if not event_type:
        raise AuditValidationError("event_type must be non-empty")
    if not actor:
        raise AuditValidationError("actor must be non-empty")
    if not _HASH_RE.match(prev_hash):
        raise AuditValidationError("prev_hash must be 64 lowercase hex characters")
    return canonical_json(
        {
            "v": SCHEMA_VERSION,
            "seq": seq,
            "occurred_at": format_timestamp(occurred_at),
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )


def hash_canonical(canonical: str) -> str:
    """sha256 over the UTF-8 bytes of the canonical text, lowercase hex."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
