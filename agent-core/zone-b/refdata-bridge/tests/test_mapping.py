"""``mapping.py``: nanos conversion and fail-closed parsing of Redis payloads."""

from __future__ import annotations

import math

import pytest

from refdata_bridge.mapping import (
    MappingError,
    parse_regime,
    parse_snapshot,
    to_nanos,
)

NOW_NS = 1_700_000_000_000_000_000
STALE_AFTER_NS = 2_000_000_000  # 2s


def snapshot_payload(**overrides: object) -> dict:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "ingestion_ts_ns": NOW_NS,
        "mid_price": 150.025,
        "adv_30d": 1_234_567.0,
        "is_stale": False,
        "warmup": False,
    }
    return {**base, **overrides}


def regime_payload(**overrides: object) -> dict:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "label": "TRENDING_BULL",
        "confidence": 0.87,
        "ts_ns": NOW_NS,
    }
    return {**base, **overrides}


# ── to_nanos ──────────────────────────────────────────────────────────────


def test_to_nanos_exact_for_two_decimals() -> None:
    assert to_nanos(150.02, "mid_price") == 150_020_000_000


def test_to_nanos_half_even_rounding_at_ninth_decimal() -> None:
    # 0.1234567895 -> half-even rounds the trailing 5 to the nearest even digit (8 -> stays 8...
    # verify against Decimal directly rather than hand-picking a case prone to float noise).
    from decimal import ROUND_HALF_EVEN, Decimal

    value = 1.0000000005
    expected = int((Decimal(str(value)) * Decimal(10) ** 9).to_integral_value(ROUND_HALF_EVEN))
    assert to_nanos(value, "x") == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_to_nanos_rejects_non_finite(bad: float) -> None:
    with pytest.raises(MappingError):
        to_nanos(bad, "mid_price")


def test_to_nanos_rejects_negative() -> None:
    with pytest.raises(MappingError):
        to_nanos(-1.0, "mid_price")


def test_to_nanos_never_uses_plain_float_multiplication_error() -> None:
    # 0.1 + 0.2 style drift must not leak through: Decimal(str(x)) uses the
    # shortest repr, matching what the JSON producer actually wrote.
    assert to_nanos(0.1, "x") == 100_000_000


# ── parse_snapshot ───────────────────────────────────────────────────────


def test_parse_snapshot_maps_fresh_confident_snapshot() -> None:
    data = parse_snapshot(snapshot_payload(), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS)
    assert data.symbol == "AAPL"
    assert data.mid_price_nanos == 150_025_000_000
    assert data.adv_30d_nanos == 1_234_567_000_000_000
    assert data.as_of_ns == NOW_NS
    assert data.is_stale is False


def test_parse_snapshot_rejects_missing_symbol() -> None:
    payload = snapshot_payload()
    del payload["symbol"]
    with pytest.raises(MappingError):
        parse_snapshot(payload, now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS)


def test_parse_snapshot_rejects_bad_symbol_grammar() -> None:
    with pytest.raises(MappingError):
        parse_snapshot(
            snapshot_payload(symbol="aapl"), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS
        )


def test_parse_snapshot_rejects_missing_price_fields() -> None:
    payload = snapshot_payload()
    del payload["mid_price"]
    with pytest.raises(MappingError):
        parse_snapshot(payload, now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS)


def test_parse_snapshot_rejects_non_finite_mid_price() -> None:
    with pytest.raises(MappingError):
        parse_snapshot(
            snapshot_payload(mid_price=math.nan), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS
        )


def test_parse_snapshot_rejects_non_positive_adv() -> None:
    with pytest.raises(MappingError):
        parse_snapshot(
            snapshot_payload(adv_30d=0.0), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS
        )


def test_parse_snapshot_marks_stale_when_source_flags_it() -> None:
    data = parse_snapshot(
        snapshot_payload(is_stale=True), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS
    )
    assert data.is_stale is True


def test_parse_snapshot_marks_stale_when_still_warming_up() -> None:
    data = parse_snapshot(
        snapshot_payload(warmup=True), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS
    )
    assert data.is_stale is True, "not confident yet: must not claim fresh"


def test_parse_snapshot_marks_stale_when_older_than_bridge_threshold() -> None:
    old_ts = NOW_NS - 3_000_000_000  # 3s > 2s threshold
    data = parse_snapshot(
        snapshot_payload(ingestion_ts_ns=old_ts), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS
    )
    assert data.is_stale is True


def test_parse_snapshot_marks_stale_when_from_the_future() -> None:
    future_ts = NOW_NS + 3_000_000_000
    data = parse_snapshot(
        snapshot_payload(ingestion_ts_ns=future_ts), now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS
    )
    assert data.is_stale is True


def test_parse_snapshot_defaults_warmup_missing_to_stale() -> None:
    payload = snapshot_payload()
    del payload["warmup"]
    data = parse_snapshot(payload, now_ns=NOW_NS, stale_after_ns=STALE_AFTER_NS)
    assert data.is_stale is True, "absent warmup must not be guessed confident"


# ── parse_regime ─────────────────────────────────────────────────────────


def test_parse_regime_maps_valid_message() -> None:
    data = parse_regime(regime_payload())
    assert data.label == "TRENDING_BULL"
    assert data.confidence == 0.87
    assert data.timestamp_ns == NOW_NS
    assert data.state_index == 0


def test_parse_regime_rejects_unknown_label() -> None:
    with pytest.raises(MappingError):
        parse_regime(regime_payload(label="MOON"))


@pytest.mark.parametrize("bad_confidence", [-0.1, 1.5, math.nan])
def test_parse_regime_rejects_bad_confidence(bad_confidence: float) -> None:
    with pytest.raises(MappingError):
        parse_regime(regime_payload(confidence=bad_confidence))


def test_parse_regime_rejects_missing_ts() -> None:
    payload = regime_payload()
    del payload["ts_ns"]
    with pytest.raises(MappingError):
        parse_regime(payload)
