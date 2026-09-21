"""AegisDecision -> motor -> (paper) Alpaca, end to end over httpx.MockTransport. No network."""

from __future__ import annotations

from decimal import Decimal
from types import ModuleType
from typing import Any

import httpx
import pytest
from execution_motor.alpaca import AlpacaPaperBroker
from execution_motor.config import MotorConfig
from execution_motor.halt import KillStateHaltSource, KillSwitch
from execution_motor.models import ExecutionReport, ExecutionStatus, RejectReason
from execution_motor.motor import ExecutionMotor
from execution_motor.service import handle_decision, to_aegis_execution_report, to_nanos
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy

from .helpers import NOW_NS, HmacTestVerifier
from .test_alpaca_http import CID, CREDS, Script, jresp, order_json, timeout
from .test_proto_adapter import build

D = Decimal


def motor_over(script: Script, kill: KillSwitch | None = None) -> ExecutionMotor:
    broker = AlpacaPaperBroker(
        CREDS,
        transport=httpx.MockTransport(script),
        sleep=lambda _s: None,
        rng=lambda: 0.0,
        read_retries=1,
    )
    return ExecutionMotor(
        config=MotorConfig(D("25000"), D("100000"), environment="test"),
        brokers={broker.venue: broker},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=kill or KillSwitch(start_halted=False),
        verifier=HmacTestVerifier(),
        clock_ns=lambda: NOW_NS,
    )


def decision(pb2: dict[str, ModuleType], **over: Any) -> Any:
    order, att = build(pb2)
    msg = pb2["aegis"].AegisDecision(
        signal_id="sig-1", decision=1, attestation=att, order=order, decided_at_ns=NOW_NS
    )
    for k, v in over.items():
        setattr(msg, k, v)
    return msg


def test_approved_decision_reaches_paper_broker_once(pb2: dict[str, ModuleType]) -> None:
    script = Script(jresp(200, order_json()))
    report = handle_decision(motor_over(script), decision(pb2), now_ns=NOW_NS)
    assert report.status is ExecutionStatus.ACCEPTED
    assert [r.url.host for r in script.requests] == ["paper-api.alpaca.markets"]
    assert script.methods() == ["POST"]


def test_timeout_then_reconcile_reports_broker_truth(pb2: dict[str, ModuleType]) -> None:
    filled = order_json(
        status="filled", filled_qty="10", filled_avg_price="190.5", filled_at="2026-09-20T10:00:01Z"
    )
    script = Script(timeout(), jresp(200, filled))
    report = handle_decision(motor_over(script), decision(pb2), now_ns=NOW_NS)
    assert report.status is ExecutionStatus.FILLED
    assert report.avg_fill_price == D("190.5")
    assert script.methods() == ["POST", "GET"]


def test_timeout_and_not_found_is_unknown_halts_and_blocks_replay(
    pb2: dict[str, ModuleType],
) -> None:
    script = Script(timeout(), jresp(404, {"message": "not found"}))
    kill = KillSwitch(start_halted=False)
    motor = motor_over(script, kill)
    first = handle_decision(motor, decision(pb2), now_ns=NOW_NS)
    assert first.status is ExecutionStatus.UNKNOWN
    assert kill.is_halted()
    second = handle_decision(motor, decision(pb2), now_ns=NOW_NS)
    assert second.reject_reason is RejectReason.HALTED
    assert script.methods() == ["POST", "GET"]  # never resubmitted


def test_definite_broker_reject_is_reported(pb2: dict[str, ModuleType]) -> None:
    script = Script(jresp(403, {"message": "insufficient buying power"}))
    report = handle_decision(motor_over(script), decision(pb2), now_ns=NOW_NS)
    assert report.reject_reason is RejectReason.BROKER_REJECTED


def test_default_motor_denies_decision(pb2: dict[str, ModuleType]) -> None:
    script = Script()
    motor = ExecutionMotor(
        config=MotorConfig(D("25000"), D("100000"), environment="test"),
        brokers={"v": AlpacaPaperBroker(CREDS, transport=httpx.MockTransport(script))},
        router=SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.ALLOW)),
        kill_switch=KillSwitch(start_halted=False),
        clock_ns=lambda: NOW_NS,
    )
    report = handle_decision(motor, decision(pb2), now_ns=NOW_NS)
    assert report.reject_reason is RejectReason.ATTESTATION_INVALID
    assert script.requests == []


@pytest.mark.parametrize("status", [0, 2, 3])
def test_non_approved_decision_is_refused(pb2: dict[str, ModuleType], status: int) -> None:
    script = Script()
    report = handle_decision(motor_over(script), decision(pb2, decision=status), now_ns=NOW_NS)
    assert report.reject_reason is RejectReason.INVALID_ORDER
    assert script.requests == []


def test_approved_without_attestation_or_order_is_refused(pb2: dict[str, ModuleType]) -> None:
    msg = decision(pb2)
    msg.ClearField("attestation")
    assert (
        handle_decision(motor_over(Script()), msg, now_ns=NOW_NS).reject_reason
        is RejectReason.INVALID_ORDER
    )
    msg2 = decision(pb2)
    msg2.ClearField("order")
    assert (
        handle_decision(motor_over(Script()), msg2, now_ns=NOW_NS).reject_reason
        is RejectReason.INVALID_ORDER
    )


def test_malformed_order_in_decision_is_refused(pb2: dict[str, ModuleType]) -> None:
    order, att = build(pb2, order_over={"quantity_nanos": -1})
    msg = pb2["aegis"].AegisDecision(signal_id="s", decision=1, attestation=att, order=order)
    report = handle_decision(motor_over(Script()), msg, now_ns=NOW_NS)
    assert report.reject_reason is RejectReason.INVALID_ORDER


def test_report_to_aegis_proto(pb2: dict[str, ModuleType]) -> None:
    filled = order_json(
        status="filled",
        filled_qty="10",
        filled_avg_price="190.4999",
        filled_at="2026-09-20T10:00:01Z",
    )
    report = handle_decision(motor_over(Script(jresp(200, filled))), decision(pb2), now_ns=NOW_NS)
    msg = to_aegis_execution_report(
        pb2["aegis"].ExecutionReport, report, account_equity=D("50123.45"), reported_at_ns=NOW_NS
    )
    assert msg.order_id == CID
    assert msg.status == 3 and msg.side == 1
    assert msg.filled_qty_nanos == 10 * 10**9
    assert msg.avg_fill_price_nanos == 190_499_900_000
    assert msg.account_equity_nanos == 50_123_450_000_000


def test_nanos_conversion_rounds_half_even_and_refuses_nan() -> None:
    assert to_nanos(D("0.0000000005")) == 0
    assert to_nanos(D("0.0000000015")) == 2
    with pytest.raises(ValueError):
        to_nanos(D("NaN"))


def test_unknown_status_is_not_reported_as_terminal(pb2: dict[str, ModuleType]) -> None:
    report = ExecutionReport(
        order_id="o",
        client_order_id="o",
        signal_id="s",
        symbol="AAPL",
        status=ExecutionStatus.UNKNOWN,
        requested_quantity=D("1"),
        reject_reason=RejectReason.SUBMIT_OUTCOME_UNKNOWN,
        received_at_ns=1,
        updated_at_ns=2,
    )
    msg = to_aegis_execution_report(
        pb2["aegis"].ExecutionReport, report, account_equity=D("1"), reported_at_ns=3
    )
    assert msg.status == 1  # ORDER_SENT, never FILLED/REJECTED


def test_kill_state_source_halts_on_any_non_normal_level() -> None:
    level = {"v": 0}
    kill = KillSwitch(start_halted=False, signal_source=KillStateHaltSource(lambda: level["v"]))
    assert not kill.is_halted()
    level["v"] = 2
    assert kill.is_halted()
    level["v"] = 0
    assert kill.is_halted()  # sticky until explicit resume
    kill.resume(operator="ali")
    assert not kill.is_halted()
