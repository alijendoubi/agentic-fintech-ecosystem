"""Shared test factories and doubles. Nothing here ships in the runtime package."""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import Any

from execution_motor.models import (
    Attestation,
    AttestedOrder,
    ExecAlgo,
    Order,
    OrderType,
    Side,
)

NOW_NS = 1_800_000_000_000_000_000
TEST_KEY_ID = "test-key-1"
TEST_SECRET = b"unit-test-only-secret"


def make_order(**overrides: Any) -> Order:
    fields: dict[str, Any] = {
        "order_id": "0b9c7f3e-6d0e-4a3a-9c53-0d9d5b8e1a11",
        "signal_id": "5f1c2b7a-1111-4222-8333-444455556666",
        "symbol": "AAPL",
        "created_at_ns": NOW_NS - 1_000_000_000,
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "quantity": Decimal("10"),
        "limit_price": Decimal("190.50"),
        "stop_price": Decimal("0"),
        "preferred_venue": "",
        "max_venue_toxicity": Decimal("0"),
        "algo": ExecAlgo.DIRECT,
        "pov_target_rate": Decimal("0"),
        "iceberg_display_size": Decimal("0"),
    }
    fields.update(overrides)
    if "signal_id" not in overrides:
        fields["signal_id"] = fields["order_id"]  # Aegis contract: order_id == signal_id
    return Order(**fields)


class HmacTestVerifier:
    """TEST DOUBLE ONLY: verifies an HMAC-SHA256 tag instead of an HSM ECDSA signature.

    Lives under tests/ on purpose so it cannot be imported from the runtime package.
    """

    def __init__(self, secret: bytes = TEST_SECRET, key_id: str = TEST_KEY_ID) -> None:
        self._secret = secret
        self._key_id = key_id

    def verify(self, payload: bytes, signature: bytes, key_id: str) -> bool:
        if key_id != self._key_id:
            return False
        expected = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return hmac.compare_digest(expected, signature)


def sign(payload: bytes, secret: bytes = TEST_SECRET) -> bytes:
    return hmac.new(secret, payload, hashlib.sha256).digest()


def attest(
    order: Order,
    *,
    secret: bytes = TEST_SECRET,
    key_id: str = TEST_KEY_ID,
    decided_at_ns: int = NOW_NS - 500_000_000,
    expires_at_ns: int = NOW_NS + 4_000_000_000,
) -> AttestedOrder:
    """Build an AttestedOrder whose payload is a fixed function of the signed fields."""
    payload = (
        f"{order.signal_id}|{order.symbol}|{order.side}|{order.order_type}|{order.quantity}|"
        f"{order.limit_price}|{order.stop_price}|{decided_at_ns}|{expires_at_ns}"
    ).encode()
    return AttestedOrder(
        order=order,
        attestation=Attestation(
            signature=sign(payload, secret),
            key_id=key_id,
            signed_payload=payload,
            payload_sha256=hashlib.sha256(payload).digest(),
            decided_at_ns=decided_at_ns,
            expires_at_ns=expires_at_ns,
        ),
    )
