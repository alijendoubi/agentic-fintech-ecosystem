"""Parse sensory-array / regime-detector Redis payloads into plain (duck-typed) DTOs.

Money contract (house convention, matches ``execution_motor.service.to_nanos``):
prices/volumes are converted to int64 nanos (1e-9) via ``Decimal`` with half-even
rounding at the 9th decimal place. sensory-array's ``MarketSnapshot.mid_price`` and
``.adv_30d`` are JSON floats (see ``agent-core/zone-b/sensory-array/src/normalizer.rs``);
this module is the ONLY place that turns them into fixed-point nanos, deliberately,
never with plain float math.

Fail-closed rules (never guess a value is fresh):
* a snapshot missing a required field, or with a non-finite / non-positive
  mid_price or adv_30d, is REJECTED (not sent, not silently zeroed);
* a snapshot is marked ``is_stale=True`` (not rejected) when the source already
  flagged it stale, when it is still in ``warmup`` (Z/MAD/vol not meaningful
  yet -- see normalizer.rs), or when this bridge's own clock judges it too old
  (``snapshot_stale_after_s``);
* a regime payload missing a required field or with an unknown label / bad
  confidence is REJECTED. ``RegimeLabelPacket`` (market_snapshot.proto) has no
  ``state_index`` in the wire payload regime-detector actually publishes
  (``regime_detector/publisher.py`` only ever writes symbol/label/confidence/ts_ns),
  so ``state_index`` is always forwarded as 0 -- a known limitation, not a real
  state index; see README "Known limitations".
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

SYMBOL_PATTERN = re.compile(r"[A-Z.\-]{1,10}")

# market_snapshot.proto RegimeLabel enum values (byte-for-byte; guarded by
# regime_detector/tests/test_labels.py upstream, mirrored here since this
# package must not import the Rust/Python enum definitions).
KNOWN_REGIME_LABELS = frozenset(
    {
        "REGIME_UNKNOWN",
        "TRENDING_BULL",
        "TRENDING_BEAR",
        "HIGH_VOL_CHOP",
        "LOW_VOL_CHOP",
        "CRISIS",
    }
)

_NANOS = Decimal(10) ** 9


class MappingError(ValueError):
    """A payload could not be safely mapped; the caller must skip and log it."""


def to_nanos(value: float, name: str) -> int:
    """Float dollars/shares -> int64 nanos, half-even at the 9th decimal.

    Refuses non-finite or negative values (money/volume can't be negative here).
    """
    if not isinstance(value, int | float) or not math.isfinite(value):
        raise MappingError(f"{name} is not a finite number: {value!r}")
    try:
        decimal_value = Decimal(str(value))
    except InvalidOperation as exc:
        raise MappingError(f"{name} is not a valid decimal: {value!r}") from exc
    if decimal_value < 0:
        raise MappingError(f"{name} must not be negative: {value!r}")
    return int((decimal_value * _NANOS).to_integral_value(rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True, slots=True)
class ReferenceSnapshotData:
    """Plain DTO mirroring ``afe.shared.ReferenceSnapshot`` (aegis.proto)."""

    symbol: str
    mid_price_nanos: int
    adv_30d_nanos: int
    as_of_ns: int
    is_stale: bool


@dataclass(frozen=True, slots=True)
class RegimeLabelData:
    """Plain DTO mirroring ``afe.shared.RegimeLabelPacket`` (market_snapshot.proto)."""

    symbol: str
    label: str
    confidence: float
    timestamp_ns: int
    state_index: int = 0


def parse_snapshot(payload: dict, *, now_ns: int, stale_after_ns: int) -> ReferenceSnapshotData:
    """Map one sensory-array ``MarketSnapshot`` JSON dict to a ``ReferenceSnapshotData``.

    Raises ``MappingError`` for a malformed/incomplete snapshot (skip, don't guess).
    Never raises for a merely-stale one: staleness sets ``is_stale=True`` instead.
    """
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise MappingError(f"invalid or missing symbol: {symbol!r}")
    as_of_ns = payload.get("ingestion_ts_ns")
    if not isinstance(as_of_ns, int) or as_of_ns <= 0:
        raise MappingError(f"invalid or missing ingestion_ts_ns: {as_of_ns!r}")
    mid_price = payload.get("mid_price")
    adv_30d = payload.get("adv_30d")
    if mid_price is None or adv_30d is None:
        raise MappingError("snapshot missing mid_price or adv_30d")
    mid_price_nanos = to_nanos(mid_price, "mid_price")
    adv_30d_nanos = to_nanos(adv_30d, "adv_30d")
    if mid_price_nanos <= 0 or adv_30d_nanos <= 0:
        raise MappingError("mid_price and adv_30d must be strictly positive")

    source_stale = bool(payload.get("is_stale", False))
    warmup = bool(payload.get("warmup", True))  # default: not confident if absent
    age_ns = now_ns - as_of_ns
    clock_stale = age_ns < 0 or age_ns > stale_after_ns
    is_stale = source_stale or warmup or clock_stale

    return ReferenceSnapshotData(
        symbol=symbol,
        mid_price_nanos=mid_price_nanos,
        adv_30d_nanos=adv_30d_nanos,
        as_of_ns=as_of_ns,
        is_stale=is_stale,
    )


def parse_regime(payload: dict) -> RegimeLabelData:
    """Map one regime-detector ``regime:labels`` JSON dict to a ``RegimeLabelData``.

    Raises ``MappingError`` for a malformed payload, including a missing or invalid
    ``symbol`` (ALI-158: labels are per symbol; a symbol-less label is refused
    rather than guessed). Confidence is not adjusted for
    staleness here: the caller tracks per-message freshness and drops stale entries
    (``regime_stale_after_s``) rather than forwarding a guessed value.
    """
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise MappingError(f"invalid or missing symbol: {symbol!r}")
    label = payload.get("label")
    if not isinstance(label, str) or label not in KNOWN_REGIME_LABELS:
        raise MappingError(f"unknown or missing regime label: {label!r}")
    confidence = payload.get("confidence")
    if not isinstance(confidence, int | float) or not math.isfinite(confidence):
        raise MappingError(f"confidence is not a finite number: {confidence!r}")
    if not 0.0 <= confidence <= 1.0:
        raise MappingError(f"confidence outside [0, 1]: {confidence!r}")
    ts_ns = payload.get("ts_ns")
    if not isinstance(ts_ns, int) or ts_ns <= 0:
        raise MappingError(f"invalid or missing ts_ns: {ts_ns!r}")
    return RegimeLabelData(
        symbol=symbol,
        label=label,
        confidence=float(confidence),
        timestamp_ns=ts_ns,
        state_index=0,
    )
