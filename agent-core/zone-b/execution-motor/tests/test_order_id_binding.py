"""order_id must equal signal_id (Aegis binds only signal_id in the signed text)."""

from __future__ import annotations

from types import ModuleType

import pytest
from execution_motor.errors import OrderValidationError
from execution_motor.models import ExecutionStatus
from execution_motor.proto_adapter import attested_order_from_proto
from execution_motor.service import handle_decision

from .aegis_rig import FX_NOW_NS, decision_from_fixture, fixture_motor
from .test_aegis_fixtures import load_fixture, to_messages
from .test_motor import FakeBroker
from .test_proto_adapter import build


def test_order_id_must_equal_signal_id_at_the_adapter(pb2: dict[str, ModuleType]) -> None:
    order, att = build(pb2, {"order_id": "some-other-id"})
    with pytest.raises(OrderValidationError, match="order_id must equal signal_id"):
        attested_order_from_proto(order, att)


def test_real_aegis_orders_satisfy_the_binding(pb2: dict[str, ModuleType]) -> None:
    for case in load_fixture()["cases"].values():
        assert case["order"]["order_id"] == case["order"]["signal_id"]


def test_relabelled_real_aegis_order_never_reaches_the_broker(pb2: dict[str, ModuleType]) -> None:
    """Swap the client order id on a genuinely signed order: refused, broker untouched."""
    decision = decision_from_fixture(pb2, "buy")
    decision.order.order_id = "attacker-chosen-id"
    broker = FakeBroker()
    report = handle_decision(
        fixture_motor({"alpaca-paper": broker}), decision, now_ns=FX_NOW_NS, reference_price=None
    )
    assert report.status is ExecutionStatus.REJECTED
    assert broker.submitted == []


def test_client_order_id_sent_to_broker_is_the_signal_id(pb2: dict[str, ModuleType]) -> None:
    broker = FakeBroker()
    order, _ = to_messages(pb2, load_fixture()["cases"]["buy"])
    report = handle_decision(
        fixture_motor({"alpaca-paper": broker}),
        decision_from_fixture(pb2, "buy"),
        now_ns=FX_NOW_NS,
        reference_price=None,
    )
    assert report.status is ExecutionStatus.FILLED
    assert broker.submitted[0].client_order_id == order.signal_id
