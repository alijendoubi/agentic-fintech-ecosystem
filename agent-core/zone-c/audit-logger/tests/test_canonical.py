import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from afe_audit.canonical import (
    GENESIS_HASH,
    build_canonical,
    canonical_json,
    format_timestamp,
    hash_canonical,
)
from afe_audit.errors import AuditValidationError


def test_keys_sorted_and_no_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, 2, {"z": None, "y": True}]}) == (
        '{"a":[1,2,{"y":true,"z":null}],"b":1}'
    )


def test_key_order_of_input_does_not_change_output() -> None:
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})


def test_non_ascii_is_escaped_so_output_is_pure_ascii() -> None:
    out = canonical_json({"k": "é€😀"})
    assert out == '{"k":"\\u00e9\\u20ac\\ud83d\\ude00"}'
    assert out.isascii()


def test_no_unicode_normalisation_is_applied() -> None:
    composed = "é"
    decomposed = "é"
    assert canonical_json({"k": composed}) != canonical_json({"k": decomposed})


@pytest.mark.parametrize("bad", [1.5, float("nan"), float("inf"), 0.0])
def test_floats_are_rejected(bad: float) -> None:
    with pytest.raises(AuditValidationError, match="float"):
        canonical_json({"x": bad})


@pytest.mark.parametrize("bad", ["a\x00b", "\ud800"])
def test_nul_and_lone_surrogates_are_rejected(bad: str) -> None:
    with pytest.raises(AuditValidationError):
        canonical_json({"x": bad})


def test_int_range_is_enforced_and_bool_is_not_int() -> None:
    assert canonical_json({"x": 2**63 - 1, "t": True}) == '{"t":true,"x":9223372036854775807}'
    with pytest.raises(AuditValidationError, match="int64"):
        canonical_json({"x": 2**63})
    with pytest.raises(AuditValidationError, match="int64"):
        canonical_json({"x": -(2**63) - 1})


def test_non_string_keys_and_unknown_types_are_rejected() -> None:
    with pytest.raises(AuditValidationError, match="key"):
        canonical_json({1: "a"})
    with pytest.raises(AuditValidationError, match="unsupported"):
        canonical_json({"x": object()})
    with pytest.raises(AuditValidationError, match="unsupported"):
        canonical_json({"x": {1, 2}})


def test_excessive_depth_and_cycles_are_rejected() -> None:
    deep: dict[str, object] = {}
    cur = deep
    for _ in range(100):
        nxt: dict[str, object] = {}
        cur["a"] = nxt
        cur = nxt
    with pytest.raises(AuditValidationError, match="depth"):
        canonical_json(deep)
    cyc: dict[str, object] = {}
    cyc["self"] = cyc
    with pytest.raises(AuditValidationError, match="depth"):
        canonical_json(cyc)


def test_tuples_serialise_as_arrays() -> None:
    assert canonical_json({"x": (1, 2)}) == '{"x":[1,2]}'


def test_timestamp_is_utc_fixed_microseconds() -> None:
    ts = datetime(2026, 9, 19, 12, 0, 0, 5, tzinfo=UTC)
    assert format_timestamp(ts) == "2026-09-19T12:00:00.000005Z"
    plus2 = datetime(2026, 9, 19, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert format_timestamp(plus2) == "2026-09-19T12:00:00.000000Z"
    with pytest.raises(AuditValidationError, match="timezone"):
        format_timestamp(datetime(2026, 9, 19, 12, 0, 0))


def test_build_canonical_is_stable_and_binds_all_fields() -> None:
    ts = datetime(2026, 9, 19, tzinfo=UTC)
    c = build_canonical(1, ts, "t", "a", {"k": "v"}, GENESIS_HASH)
    assert c == (
        '{"actor":"a","event_type":"t","occurred_at":"2026-09-19T00:00:00.000000Z",'
        '"payload":{"k":"v"},"prev_hash":"' + "0" * 64 + '","seq":1,"v":1}'
    )
    assert hash_canonical(c) == hashlib.sha256(c.encode("utf-8")).hexdigest()
    variants = [
        build_canonical(2, ts, "t", "a", {"k": "v"}, GENESIS_HASH),
        build_canonical(1, ts, "u", "a", {"k": "v"}, GENESIS_HASH),
        build_canonical(1, ts, "t", "b", {"k": "v"}, GENESIS_HASH),
        build_canonical(1, ts, "t", "a", {"k": "w"}, GENESIS_HASH),
        build_canonical(1, ts, "t", "a", {"k": "v"}, "1" * 64),
        build_canonical(1, ts + timedelta(microseconds=1), "t", "a", {"k": "v"}, GENESIS_HASH),
    ]
    assert len({hash_canonical(v) for v in variants} | {hash_canonical(c)}) == 7


def test_build_canonical_rejects_bad_prev_hash_and_empty_fields() -> None:
    ts = datetime(2026, 9, 19, tzinfo=UTC)
    with pytest.raises(AuditValidationError, match="prev_hash"):
        build_canonical(1, ts, "t", "a", {}, "xyz")
    with pytest.raises(AuditValidationError, match="event_type"):
        build_canonical(1, ts, "", "a", {}, GENESIS_HASH)
    with pytest.raises(AuditValidationError, match="actor"):
        build_canonical(1, ts, "t", "", {}, GENESIS_HASH)
    with pytest.raises(AuditValidationError, match="seq"):
        build_canonical(0, ts, "t", "a", {}, GENESIS_HASH)
