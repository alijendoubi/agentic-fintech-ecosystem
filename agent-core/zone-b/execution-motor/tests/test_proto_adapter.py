from __future__ import annotations

from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest

from execution_motor.errors import OrderValidationError
from execution_motor.models import ExecAlgo, OrderType, Side
from execution_motor.proto_adapter import (
    SIGNING_DOMAIN,
    attested_order_from_proto,
    canonical_signing_payload,
)

from .helpers import NOW_NS


def _msg(pb2: ModuleType, **overrides: Any) -> Any:
    msg = pb2.OrderRequest(
        order_id="0b9c7f3e-6d0e-4a3a-9c53-0d9d5b8e1a11",
        signal_id="sig-1",
        symbol="AAPL",
        created_at_ns=NOW_NS,
        side=pb2.ORDER_BUY,
        order_type=pb2.LIMIT,
        quantity=10.0,
        limit_price=190.5,
        hsm_signature=b"\x01\x02",
        hsm_key_id="key-1",
        algo=pb2.DIRECT,
    )
    for name, value in overrides.items():
        setattr(msg, name, value)
    return msg


def test_conversion_maps_fields_and_uses_decimal(order_pb2: ModuleType) -> None:
    attested = attested_order_from_proto(_msg(order_pb2))
    order = attested.order
    assert order.symbol == "AAPL"
    assert order.side is Side.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.algo is ExecAlgo.DIRECT
    assert order.quantity == Decimal("10")
    assert order.limit_price == Decimal("190.5")
    assert isinstance(order.limit_price, Decimal)
    assert attested.attestation.key_id == "key-1"
    assert attested.attestation.signature == b"\x01\x02"


def test_missing_signature_is_kept_empty_not_an_error(order_pb2: ModuleType) -> None:
    attested = attested_order_from_proto(_msg(order_pb2, hsm_signature=b"", hsm_key_id=""))
    assert attested.attestation.signature == b""
    assert attested.attestation.key_id == ""


def test_payload_excludes_signature_and_status_but_binds_everything_else(
    order_pb2: ModuleType,
) -> None:
    base = canonical_signing_payload(_msg(order_pb2))
    assert base.startswith(SIGNING_DOMAIN)
    assert canonical_signing_payload(_msg(order_pb2, hsm_signature=b"other")) == base
    assert canonical_signing_payload(_msg(order_pb2, status=order_pb2.ORDER_SENT)) == base
    assert canonical_signing_payload(_msg(order_pb2, quantity=11.0)) != base
    assert canonical_signing_payload(_msg(order_pb2, symbol="MSFT")) != base
    assert canonical_signing_payload(_msg(order_pb2, hsm_key_id="key-2")) != base


def test_payload_is_stable_across_calls(order_pb2: ModuleType) -> None:
    assert canonical_signing_payload(_msg(order_pb2)) == canonical_signing_payload(_msg(order_pb2))


def test_conversion_does_not_mutate_message(order_pb2: ModuleType) -> None:
    msg = _msg(order_pb2)
    before = msg.SerializeToString()
    attested_order_from_proto(msg)
    assert msg.SerializeToString() == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"side": 0},
        {"side": 99},
        {"order_type": 0},
        {"algo": 0},
        {"quantity": 0.0},
        {"quantity": float("nan")},
        {"quantity": float("inf")},
        {"limit_price": -1.0},
        {"symbol": ""},
        {"order_id": ""},
        {"created_at_ns": 0},
    ],
)
def test_invalid_messages_raise_validation_error(
    order_pb2: ModuleType, overrides: dict[str, Any]
) -> None:
    with pytest.raises(OrderValidationError):
        attested_order_from_proto(_msg(order_pb2, **overrides))


def test_non_pending_status_is_refused(order_pb2: ModuleType) -> None:
    with pytest.raises(OrderValidationError):
        attested_order_from_proto(_msg(order_pb2, status=order_pb2.ORDER_FILLED))
