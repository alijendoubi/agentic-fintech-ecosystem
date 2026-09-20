"""Immutable value objects for the audit chain."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from afe_audit.canonical import canonical_json
from afe_audit.errors import AuditValidationError

MAX_LABEL_LENGTH = 200


def _check_label(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AuditValidationError(f"{name} must be a non-empty string")
    if len(value) > MAX_LABEL_LENGTH:
        raise AuditValidationError(f"{name} exceeds {MAX_LABEL_LENGTH} characters")
    if "\x00" in value:
        raise AuditValidationError(f"{name} contains NUL")


@dataclass(frozen=True)
class AuditEvent:
    """What a caller wants recorded. The payload is validated and frozen (as canonical JSON) at
    construction,
    so later mutation of the caller's dict cannot change what gets hashed."""

    event_type: str
    actor: str
    payload: Mapping[str, object]
    payload_json: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _check_label("event_type", self.event_type)
        _check_label("actor", self.actor)
        if not isinstance(self.payload, Mapping):
            raise AuditValidationError("payload must be a mapping")
        frozen = canonical_json(self.payload)
        object.__setattr__(self, "payload_json", frozen)
        object.__setattr__(self, "payload", json.loads(frozen))


@dataclass(frozen=True)
class AuditRecord:
    """A committed row of the chain."""

    seq: int
    occurred_at: datetime
    event_type: str
    actor: str
    prev_hash: str
    hash: str
    canonical: str

    @property
    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.canonical)["payload"]
        return payload


@dataclass(frozen=True)
class ChainBreak:
    """The first defect found by verification."""

    seq: int
    defect: str
    expected: str | None = None
    actual: str | None = None


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    rows_checked: int
    first_break: ChainBreak | None
    head_seq: int
    head_hash: str


@dataclass(frozen=True)
class Anchor:
    """The chain head at a point in time, published to an external sink."""

    seq: int
    hash: str
    emitted_at: str
