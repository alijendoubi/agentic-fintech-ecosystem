from __future__ import annotations

import hashlib
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest

from execution_motor.attestation import evaluate_attestation
from execution_motor.errors import OrderValidationError
from execution_motor.models import ExecAlgo, OrderType, Side
from execution_motor.proto_adapter import (
    attested_order_from_proto,
    canonical_attestation_text,
    from_nanos,
)

from .helpers import NOW_NS, HmacTestVerifier, sign

NANO = 1_000_000_000
KEY_ID = "test-key-1"


def build(
    pb2: dict[str, ModuleType],
    order_over: dict[str, Any] | None = None,
    att_over: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    order = pb2["order"].OrderRequest(
        order_id="0b9c7f3e-6d0e-4a3a-9c53-0d9d5b8e1a11",
        signal_id="sig-1",
        symbol="AAPL",
        created_at_ns=NOW_NS,
        side=pb2["order"].ORDER_BUY,
        order_type=pb2["order"].LIMIT,
        quantity_nanos=10 * NANO,
        limit_price_nanos=190_500_000_000,
        algo=pb2["order"].DIRECT,
        hsm_key_id=KEY_ID,
        attestation_expires_at_ns=NOW_NS + 4 * NANO,
    )
    att = pb2["aegis"].Attestation(
        canonical_version="afe-attest-v1",
        key_id=KEY_ID,
        decided_at_ns=NOW_NS - 1,
        expires_at_ns=NOW_NS + 4 * NANO,
        aegis_state_seq=7,
        limits_config_sha256="ab" * 32,
    )
    for name, value in (order_over or {}).items():
        setattr(order, name, value)
    for name, value in (att_over or {}).items():
        setattr(att, name, value)
    try:
        text = canonical_attestation_text(order, att)
    except OrderValidationError:
        return order, att  # deliberately malformed enum: nothing to sign
    att.payload_sha256 = hashlib.sha256(text).digest()
    att.signature = sign(text)
    order.hsm_signature = att.signature
    return order, att


def test_from_nanos_is_exact() -> None:
    assert from_nanos(190_500_000_000, "x") == Decimal("190.5")
    assert from_nanos(1, "x") == Decimal("0.000000001")
    assert from_nanos(0, "x") == Decimal("0")
    with pytest.raises(OrderValidationError):
        from_nanos(-1, "x")


def test_conversion_maps_fields_with_exact_decimals(pb2: dict[str, ModuleType]) -> None:
    attested = attested_order_from_proto(*build(pb2))
    order = attested.order
    assert (order.symbol, order.side, order.order_type) == ("AAPL", Side.BUY, OrderType.LIMIT)
    assert order.algo is ExecAlgo.DIRECT
    assert order.quantity == Decimal("10")
    assert order.limit_price == Decimal("190.5")
    assert attested.attestation.key_id == KEY_ID
    assert attested.attestation.expires_at_ns == NOW_NS + 4 * NANO


def test_full_pipeline_verifies_with_test_verifier(pb2: dict[str, ModuleType]) -> None:
    attested = attested_order_from_proto(*build(pb2))
    assert evaluate_attestation(HmacTestVerifier(), attested) is None


def test_canonical_text_matches_the_assumed_v1_layout(pb2: dict[str, ModuleType]) -> None:
    text = canonical_attestation_text(*build(pb2)).decode()
    assert text == (
        "afe-attest-v1\n"
        "signal_id=sig-1\n"
        "symbol=AAPL\n"
        "side=BUY\n"
        "order_type=LIMIT\n"
        f"qty_nanos={10 * NANO}\n"
        "limit_price_nanos=190500000000\n"
        "stop_price_nanos=0\n"
        f"decided_at_ns={NOW_NS - 1}\n"
        f"expires_at_ns={NOW_NS + 4 * NANO}\n"
        "aegis_state_seq=7\n"
        f"limits_config_sha256={'ab' * 32}\n"
        f"key_id={KEY_ID}\n"
    )


def test_changing_a_signed_field_after_signing_breaks_verification(
    pb2: dict[str, ModuleType],
) -> None:
    order, att = build(pb2)
    order.quantity_nanos = 11 * NANO  # tamper after signing
    attested = attested_order_from_proto(order, att)
    assert evaluate_attestation(HmacTestVerifier(), attested) is not None


def test_deprecated_double_fields_are_ignored(pb2: dict[str, ModuleType]) -> None:
    order, att = build(pb2, order_over={"quantity": 999999.0, "limit_price": 0.01})
    assert attested_order_from_proto(order, att).order.quantity == Decimal("10")


def test_unsigned_hints_do_not_affect_verification(pb2: dict[str, ModuleType]) -> None:
    order, att = build(pb2, order_over={"preferred_venue": "X", "max_venue_toxicity": 0.2})
    attested = attested_order_from_proto(order, att)
    assert attested.order.preferred_venue == "X"
    assert attested.order.max_venue_toxicity == Decimal("0.2")
    assert evaluate_attestation(HmacTestVerifier(), attested) is None


def test_missing_signature_kept_empty(pb2: dict[str, ModuleType]) -> None:
    order, att = build(pb2)
    order.hsm_signature = b""
    att.signature = b""
    attested = attested_order_from_proto(order, att)
    assert attested.attestation.signature == b""


@pytest.mark.parametrize(
    "order_over",
    [
        {"side": 0},
        {"side": 99},
        {"order_type": 0},
        {"algo": 0},
        {"quantity_nanos": 0},
        {"quantity_nanos": -5},
        {"limit_price_nanos": -1},
        {"limit_price_nanos": 0},  # LIMIT without a price
        {"symbol": ""},
        {"order_id": ""},
        {"created_at_ns": 0},
        {"max_venue_toxicity": float("nan")},
        {"max_venue_toxicity": float("inf")},
    ],
)
def test_invalid_orders_raise_validation_error(
    pb2: dict[str, ModuleType], order_over: dict[str, Any]
) -> None:
    order, att = build(pb2, order_over=order_over)
    with pytest.raises(OrderValidationError):
        attested_order_from_proto(order, att)


def test_non_pending_status_refused(pb2: dict[str, ModuleType]) -> None:
    with pytest.raises(OrderValidationError):
        attested_order_from_proto(*build(pb2, order_over={"status": 3}))


def test_unsupported_canonical_version_refused(pb2: dict[str, ModuleType]) -> None:
    with pytest.raises(OrderValidationError):
        attested_order_from_proto(*build(pb2, att_over={"canonical_version": "afe-attest-v2"}))


@pytest.mark.parametrize(
    "order_over",
    [{"hsm_key_id": "other"}, {"hsm_signature": b"zzz"}, {"attestation_expires_at_ns": 5}],
)
def test_order_must_mirror_attestation(
    pb2: dict[str, ModuleType], order_over: dict[str, Any]
) -> None:
    order, att = build(pb2)
    for k, v in order_over.items():
        setattr(order, k, v)
    with pytest.raises(OrderValidationError):
        attested_order_from_proto(order, att)


def test_conversion_does_not_mutate_messages(pb2: dict[str, ModuleType]) -> None:
    order, att = build(pb2)
    before = (order.SerializeToString(), att.SerializeToString())
    attested_order_from_proto(order, att)
    assert (order.SerializeToString(), att.SerializeToString()) == before
