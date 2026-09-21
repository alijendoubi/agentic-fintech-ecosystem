"""Parse sensory-array snapshots (Redis `sensory:snapshots` JSON) into debate inputs.

The Rust sensory-array serialises its `MarketSnapshot` (zone-b/sensory-array/src/
normalizer.rs) and stamps it with the regime label/confidence it received from the regime
detector, so one message carries both the market context and the regime.

Field mapping (snapshot -> `MarketContext`): `order_flow_imbalance` -> `ofi`,
`realized_volatility` -> `realized_vol`; the rest keep their names.

Fail closed: any malformed, non-finite, stale, warm-up or regime-unknown message raises
`ContextError` and the runner drops it. Nothing here guesses or fills in a value.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .models import MarketContext, RegimeLabel

NS_PER_S = 1_000_000_000
MAX_MESSAGE_BYTES = 64 * 1024
FUTURE_TOLERANCE_NS = NS_PER_S  # clock skew allowed between sensory-array and this host


class ContextError(ValueError):
    """The message cannot be turned into a trustworthy debate input."""


@dataclass(frozen=True, slots=True)
class DebateInput:
    market_context: MarketContext
    regime: RegimeLabel
    regime_confidence: float
    ingestion_ts_ns: int


def _reject_constant(name: str) -> Any:
    raise ContextError(f"non-finite JSON constant {name}")


def _number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ContextError(f"{key} missing or not a number")
    if not math.isfinite(value):
        raise ContextError(f"{key} is not finite")
    return float(value)


def _flag(payload: dict[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ContextError(f"{key} missing or not a boolean")
    return value


def _decode(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_MESSAGE_BYTES:
            raise ContextError("message too large")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContextError("message is not valid UTF-8") from exc
    elif not isinstance(raw, str) or len(raw) > MAX_MESSAGE_BYTES:
        raise ContextError("message is not a bounded string")
    try:
        payload = json.loads(raw, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ContextError("message is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ContextError("message is not a JSON object")
    return payload


def parse_snapshot(raw: str | bytes, *, now_ns: int, max_age_s: float) -> DebateInput:
    """Validate one snapshot message. Raises `ContextError` on anything untrustworthy."""
    payload = _decode(raw)
    if _flag(payload, "is_stale"):
        raise ContextError("snapshot flagged stale by sensory-array")
    if _flag(payload, "warmup"):
        raise ContextError("snapshot is still in warm-up")
    ingestion_ns = payload.get("ingestion_ts_ns")
    if isinstance(ingestion_ns, bool) or not isinstance(ingestion_ns, int) or ingestion_ns <= 0:
        raise ContextError("ingestion_ts_ns missing or invalid")
    age_ns = now_ns - ingestion_ns
    if age_ns > max_age_s * NS_PER_S:
        raise ContextError(f"snapshot is {age_ns / NS_PER_S:.1f}s old (max {max_age_s}s)")
    if age_ns < -FUTURE_TOLERANCE_NS:
        raise ContextError("snapshot timestamp is in the future")
    label = payload.get("regime_label")
    try:
        regime = RegimeLabel(label)
    except ValueError as exc:
        raise ContextError(f"unknown regime_label {label!r}") from exc
    if regime == RegimeLabel.REGIME_UNKNOWN:
        raise ContextError("regime is REGIME_UNKNOWN")
    confidence = _number(payload, "regime_confidence")
    if not 0.0 <= confidence <= 1.0:
        raise ContextError("regime_confidence outside [0, 1]")
    try:
        context = MarketContext(
            symbol=payload.get("symbol"),  # type: ignore[arg-type]
            mid_price=_number(payload, "mid_price"),
            z_score=_number(payload, "z_score"),
            mad_score=_number(payload, "mad_score"),
            ofi=_number(payload, "order_flow_imbalance"),
            realized_vol=_number(payload, "realized_volatility"),
            adv_30d=_number(payload, "adv_30d"),
        )
    except ValidationError as exc:
        raise ContextError(f"market context invalid: {exc.error_count()} error(s)") from exc
    return DebateInput(context, regime, confidence, ingestion_ns)
