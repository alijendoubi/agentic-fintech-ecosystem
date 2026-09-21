"""ORDER_BUY <-> BUY, ORDER_SELL <-> SELL | SELL_SHORT, judged by the REAL Aegis signatures."""

from __future__ import annotations

import hashlib
from types import ModuleType

import pytest

from execution_motor.models import ExecutionStatus, RejectReason, Side
from execution_motor.proto_adapter import attested_order_from_proto
from execution_motor.service import handle_decision

from .aegis_rig import FX_NOW_NS, decision_from_fixture, fixture_motor
from .test_aegis_fixtures import load_fixture, to_messages
from .test_motor import FakeBroker


def run(pb2: dict[str, ModuleType], name: str, *, allow_short: bool) -> tuple[object, FakeBroker]:
    broker = FakeBroker()
    motor = fixture_motor({"alpaca-paper": broker}, allow_short=allow_short)
    report = handle_decision(
        motor, decision_from_fixture(pb2, name), now_ns=FX_NOW_NS, reference_price=None
    )
    return report, broker


@pytest.mark.parametrize("allow_short", [False, True])
@pytest.mark.parametrize("name", ["buy", "sell"])
def test_buy_and_sell_execute_regardless_of_short_setting(
    pb2: dict[str, ModuleType], name: str, allow_short: bool
) -> None:
    report, broker = run(pb2, name, allow_short=allow_short)
    assert report.status is ExecutionStatus.FILLED  # type: ignore[attr-defined]
    assert len(broker.submitted) == 1
    assert broker.submitted[0].side is (Side.BUY if name == "buy" else Side.SELL)


def test_signed_sell_short_is_refused_when_shorts_disabled_default(
    pb2: dict[str, ModuleType],
) -> None:
    report, broker = run(pb2, "sell_short", allow_short=False)
    assert report.status is ExecutionStatus.REJECTED  # type: ignore[attr-defined]
    assert report.reject_reason is RejectReason.ATTESTATION_INVALID  # type: ignore[attr-defined]
    assert broker.submitted == []


def test_signed_sell_short_executes_only_when_enabled(pb2: dict[str, ModuleType]) -> None:
    report, broker = run(pb2, "sell_short", allow_short=True)
    assert report.status is ExecutionStatus.FILLED  # type: ignore[attr-defined]
    assert len(broker.submitted) == 1 and broker.submitted[0].side is Side.SELL


def test_adapter_tries_sell_then_sell_short_and_records_which_matched(
    pb2: dict[str, ModuleType],
) -> None:
    cases = load_fixture()["cases"]
    got = {
        name: attested_order_from_proto(*to_messages(pb2, cases[name]), allow_short=True)
        for name in ("buy", "sell", "sell_short")
    }
    assert {n: a.attestation.attested_side for n, a in got.items()} == {
        "buy": "BUY",
        "sell": "SELL",
        "sell_short": "SELL_SHORT",
    }
    # Shorts disabled: SELL_SHORT is never a candidate, the digest cannot match.
    off = attested_order_from_proto(*to_messages(pb2, cases["sell_short"]), allow_short=False)
    assert off.attestation.attested_side == "SELL"
    assert hashlib.sha256(off.attestation.signed_payload).digest() != off.attestation.payload_sha256


def test_a_sell_attestation_is_not_a_short_and_cannot_be_flipped(
    pb2: dict[str, ModuleType],
) -> None:
    """Relabelling a SELL order as ORDER_BUY (or the reverse) breaks the digest."""
    order, att = to_messages(pb2, load_fixture()["cases"]["sell"])
    order.side = pb2["order"].ORDER_BUY
    attested = attested_order_from_proto(order, att, allow_short=True)
    assert attested.attestation.attested_side == "BUY"
    motor = fixture_motor({"alpaca-paper": FakeBroker()}, allow_short=True)
    assert motor.execute(attested).reject_reason is RejectReason.ATTESTATION_INVALID


def test_motor_denies_a_sell_short_attestation_built_directly_when_disabled(
    pb2: dict[str, ModuleType],
) -> None:
    """Defence in depth: even a pre-matched SELL_SHORT AttestedOrder is refused if disabled."""
    attested = attested_order_from_proto(
        *to_messages(pb2, load_fixture()["cases"]["sell_short"]), allow_short=True
    )
    broker = FakeBroker()
    report = fixture_motor({"alpaca-paper": broker}, allow_short=False).execute(attested)
    assert report.reject_reason is RejectReason.SHORT_NOT_PERMITTED
    assert broker.submitted == []
