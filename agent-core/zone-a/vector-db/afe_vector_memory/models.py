"""Immutable, validated record and result types."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from .errors import MemoryValidationError

MAX_TEXT_CHARS = 8000
MS_PER_DAY = 86_400_000
MAX_RETENTION_DAYS = 3650

_RECORD_ID = re.compile(r"[A-Za-z0-9._:\-]{1,128}")
_SYMBOL = re.compile(r"[A-Z0-9.\-]{1,32}")
_REGIME = re.compile(r"[A-Z_]{1,32}")


class MemoryKind(StrEnum):
    DEBATE_OUTCOME = "debate_outcome"
    POST_TRADE_REFLECTION = "post_trade_reflection"


class Outcome(StrEnum):
    PENDING = "pending"
    ABSTAIN = "abstain"
    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"
    UNKNOWN = "unknown"


def _match(pattern: re.Pattern[str], value: object, field: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise MemoryValidationError(f"{field}: {value!r} does not match {pattern.pattern}")
    return value


def _coerce_kind(value: object) -> MemoryKind:
    try:
        return MemoryKind(value)
    except ValueError as exc:
        raise MemoryValidationError(f"kind: {value!r} is not a valid MemoryKind") from exc


def _coerce_outcome(value: object) -> Outcome:
    try:
        return Outcome(value)
    except ValueError as exc:
        raise MemoryValidationError(f"outcome: {value!r} is not a valid Outcome") from exc


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One debate outcome or post-trade reflection."""

    record_id: str
    kind: MemoryKind
    text: str
    symbol: str
    regime: str
    ts_ms: int
    outcome: Outcome = Outcome.UNKNOWN
    signal_id: str = ""

    def __post_init__(self) -> None:
        _match(_RECORD_ID, self.record_id, "record_id")
        object.__setattr__(self, "kind", _coerce_kind(self.kind))
        object.__setattr__(self, "outcome", _coerce_outcome(self.outcome))
        if not isinstance(self.text, str) or not self.text.strip():
            raise MemoryValidationError("text must be a non-empty string")
        if len(self.text) > MAX_TEXT_CHARS:
            raise MemoryValidationError(f"text exceeds {MAX_TEXT_CHARS} characters")
        _match(_SYMBOL, self.symbol, "symbol")
        _match(_REGIME, self.regime, "regime")
        if isinstance(self.ts_ms, bool) or not isinstance(self.ts_ms, int) or self.ts_ms < 0:
            raise MemoryValidationError("ts_ms must be a non-negative int (epoch ms)")
        if not isinstance(self.signal_id, str) or len(self.signal_id) > 128:
            raise MemoryValidationError("signal_id must be a string of at most 128 chars")


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A query match. `distance` is cosine distance: 0 identical, 2 opposite."""

    record: MemoryRecord
    distance: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.distance):
            raise MemoryValidationError("distance must be finite")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long each kind stays queryable, measured from the record's own timestamp."""

    debate_days: int = 90
    reflection_days: int = 365

    def __post_init__(self) -> None:
        for name in ("debate_days", "reflection_days"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= MAX_RETENTION_DAYS
            ):
                raise MemoryValidationError(f"{name} must be an int in [1, {MAX_RETENTION_DAYS}]")

    def expires_at_ms(self, kind: MemoryKind, ts_ms: int) -> int:
        days = self.debate_days if kind == MemoryKind.DEBATE_OUTCOME else self.reflection_days
        return ts_ms + days * MS_PER_DAY


def validate_query_filters(
    *,
    n_results: int,
    regime: str | None,
    symbol: str | None,
    kind: MemoryKind | None,
    max_results: int,
) -> None:
    """Shared argument validation so every store rejects the same bad queries."""
    if isinstance(n_results, bool) or not isinstance(n_results, int):
        raise MemoryValidationError("n_results must be an int")
    if not 1 <= n_results <= max_results:
        raise MemoryValidationError(f"n_results must be in [1, {max_results}]")
    if regime is not None:
        _match(_REGIME, regime, "regime")
    if symbol is not None:
        _match(_SYMBOL, symbol, "symbol")
    if kind is not None:
        _coerce_kind(kind)
