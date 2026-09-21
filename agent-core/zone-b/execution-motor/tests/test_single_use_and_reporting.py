"""Single use of signal_id (persistent) and ReportExecution back to Aegis."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from execution_motor.aegis_reporter import (
    AegisReporter,
    GrpcReportTransport,
    InMemoryReportTransport,
)
from execution_motor.errors import ConfigError, IdempotencyStoreError
from execution_motor.halt import KillSwitch
from execution_motor.limits import FileIdempotencyStore
from execution_motor.models import ExecutionReport, ExecutionStatus, Fill, RejectReason, Side
from execution_motor.service import handle_decision, reached_broker

from .aegis_rig import FX_NOW_NS, decision_from_fixture, fixture_motor
from .test_motor import FakeBroker

D = Decimal
SIGNAL = "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11"


# ------------------------------------------------------------- single use, persistent


def test_file_store_claims_once_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "claims.jsonl"
    first = FileIdempotencyStore(path)
    assert first.claim("signal:a") is True
    assert first.claim("signal:a") is False
    first.close()
    second = FileIdempotencyStore(path)  # "restart"
    assert second.claim("signal:a") is False
    assert second.claim("signal:b") is True
    second.close()


def test_file_store_refuses_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "claims.jsonl"
    path.write_text('{"key": "signal:a"}\nnot json\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="corrupt"):
        FileIdempotencyStore(path)


def test_file_store_write_failure_fails_closed_and_keeps_the_key(tmp_path: Path) -> None:
    store = FileIdempotencyStore(tmp_path / "claims.jsonl")
    store.close()  # any later write fails
    with pytest.raises(IdempotencyStoreError):
        store.claim("signal:x")
    assert store.claim("signal:x") is False  # remembered in memory: no second chance


def test_replayed_signal_is_refused_after_a_motor_restart(
    pb2: dict[str, ModuleType], tmp_path: Path
) -> None:
    path = tmp_path / "claims.jsonl"
    broker = FakeBroker()
    motor1 = fixture_motor({"alpaca-paper": broker}, idempotency=FileIdempotencyStore(path))
    first = handle_decision(motor1, decision_from_fixture(pb2, "buy"), now_ns=FX_NOW_NS)
    assert first.status is ExecutionStatus.FILLED
    motor2 = fixture_motor({"alpaca-paper": broker}, idempotency=FileIdempotencyStore(path))
    again = handle_decision(motor2, decision_from_fixture(pb2, "buy"), now_ns=FX_NOW_NS)
    assert again.reject_reason is RejectReason.DUPLICATE_ORDER
    assert len(broker.submitted) == 1


def test_unpersistable_claim_halts_the_motor_and_never_submits(
    pb2: dict[str, ModuleType], tmp_path: Path
) -> None:
    store = FileIdempotencyStore(tmp_path / "claims.jsonl")
    store.close()
    broker, kill = FakeBroker(), KillSwitch(start_halted=False)
    motor = fixture_motor({"alpaca-paper": broker}, idempotency=store, kill=kill)
    report = handle_decision(motor, decision_from_fixture(pb2, "buy"), now_ns=FX_NOW_NS)
    assert report.reject_reason is RejectReason.INTERNAL_ERROR
    assert kill.is_halted() and broker.submitted == []


# ------------------------------------------------------------------- reporting


def make_report(
    status: ExecutionStatus,
    filled: str = "0",
    *,
    order_id: str = SIGNAL,
    reason: RejectReason | None = None,
) -> ExecutionReport:
    fills = (
        (Fill(quantity=D(filled), price=D("150"), timestamp_ns=1, venue="v"),) if D(filled) else ()
    )
    return ExecutionReport(
        order_id=order_id,
        client_order_id=order_id,
        signal_id=order_id,
        symbol="AAPL",
        side=Side.BUY,
        status=status,
        requested_quantity=D("10"),
        reject_reason=reason,
        fills=fills,
        received_at_ns=1,
        updated_at_ns=2,
    )


def reporter(
    pb2: dict[str, ModuleType], transport: Any, equity: Any = lambda: D("100000.5")
) -> AegisReporter:
    return AegisReporter(
        transport, pb2["aegis"].ExecutionReport, equity_provider=equity, clock_ns=lambda: 42
    )


def test_reports_carry_cumulative_fills_terminal_status_and_equity(
    pb2: dict[str, ModuleType],
) -> None:
    transport = InMemoryReportTransport()
    rep = reporter(pb2, transport)
    assert rep.report_execution(make_report(ExecutionStatus.PARTIALLY_FILLED, "4"))
    assert rep.report_execution(make_report(ExecutionStatus.FILLED, "10"))
    partial, done = transport.messages
    assert (partial.status, partial.filled_qty_nanos) == (2, 4 * 10**9)  # ORDER_PARTIAL
    assert (done.status, done.filled_qty_nanos) == (3, 10 * 10**9)  # ORDER_FILLED, cumulative
    assert {m.account_equity_nanos for m in transport.messages} == {100_000_500_000_000}
    assert done.order_id == SIGNAL and done.avg_fill_price_nanos == 150 * 10**9


def test_report_that_would_lower_cumulative_fill_or_follows_terminal_is_dropped(
    pb2: dict[str, ModuleType],
) -> None:
    transport = InMemoryReportTransport()
    rep = reporter(pb2, transport)
    assert rep.report_execution(make_report(ExecutionStatus.PARTIALLY_FILLED, "6"))
    assert not rep.report_execution(make_report(ExecutionStatus.PARTIALLY_FILLED, "3"))
    assert rep.report_execution(make_report(ExecutionStatus.FILLED, "10"))
    assert not rep.report_execution(make_report(ExecutionStatus.PARTIALLY_FILLED, "10"))
    assert len(transport.messages) == 2


def test_undelivered_report_is_kept_and_flushed(pb2: dict[str, ModuleType]) -> None:
    transport = InMemoryReportTransport()
    transport.fail_next = 3  # all three attempts fail
    rep = reporter(pb2, transport)
    assert rep.report_execution(make_report(ExecutionStatus.FILLED, "10")) is False
    assert rep.pending == 1 and transport.messages == []
    assert rep.flush() == 0
    assert len(transport.messages) == 1 and transport.messages[0].status == 3


def test_missing_equity_is_never_reported_as_zero(pb2: dict[str, ModuleType]) -> None:
    transport = InMemoryReportTransport()

    def no_equity() -> Decimal:
        raise RuntimeError("broker account unavailable")

    rep = reporter(pb2, transport, equity=no_equity)
    assert rep.report_execution(make_report(ExecutionStatus.FILLED, "10")) is False
    assert rep.report_equity() is False
    assert transport.messages == [] and rep.pending == 1


def test_equity_only_update_has_empty_order_id(pb2: dict[str, ModuleType]) -> None:
    transport = InMemoryReportTransport()
    assert reporter(pb2, transport).report_equity() is True
    msg = transport.messages[0]
    assert msg.order_id == "" and msg.account_equity_nanos == 100_000_500_000_000
    assert msg.reported_at_ns == 42


def test_grpc_transport_wraps_a_generated_stub(pb2: dict[str, ModuleType]) -> None:
    calls: list[tuple[Any, float]] = []

    class Stub:
        def ReportExecution(self, message: Any, timeout: float) -> Any:  # noqa: N802
            calls.append((message, timeout))
            return pb2["aegis"].Ack(ok=True)

    rep = reporter(pb2, GrpcReportTransport(Stub(), timeout_s=1.5))
    assert rep.report_execution(make_report(ExecutionStatus.FILLED, "10")) is True
    assert calls[0][1] == 1.5 and calls[0][0].status == 3

    class Refusing:
        def ReportExecution(self, message: Any, timeout: float) -> Any:  # noqa: N802
            return pb2["aegis"].Ack(ok=False, detail="no")

    assert reporter(pb2, GrpcReportTransport(Refusing())).report_equity() is False


def test_only_orders_that_reached_the_broker_are_reported() -> None:
    assert reached_broker(make_report(ExecutionStatus.FILLED, "1"))
    assert reached_broker(
        make_report(ExecutionStatus.UNKNOWN, reason=RejectReason.SUBMIT_OUTCOME_UNKNOWN)
    )
    assert reached_broker(
        make_report(ExecutionStatus.REJECTED, reason=RejectReason.BROKER_REJECTED)
    )
    for reason in (
        RejectReason.ATTESTATION_INVALID,
        RejectReason.ORDER_EXPIRED,
        RejectReason.HALTED,
        RejectReason.DUPLICATE_ORDER,
    ):
        assert not reached_broker(make_report(ExecutionStatus.REJECTED, reason=reason))


def test_handle_decision_reports_fills_and_skips_forged_orders(pb2: dict[str, ModuleType]) -> None:
    transport = InMemoryReportTransport()
    rep = reporter(pb2, transport)
    motor = fixture_motor({"alpaca-paper": FakeBroker()})
    report = handle_decision(
        motor, decision_from_fixture(pb2, "buy"), now_ns=FX_NOW_NS, reporter=rep
    )
    assert report.status is ExecutionStatus.FILLED
    assert len(transport.messages) == 1 and transport.messages[0].order_id == SIGNAL

    forged = decision_from_fixture(pb2, "sell")
    forged.order.quantity_nanos += 1  # tampered after signing
    other = handle_decision(motor, forged, now_ns=FX_NOW_NS, reporter=rep)
    assert other.reject_reason is RejectReason.ATTESTATION_INVALID
    assert len(transport.messages) == 1  # nothing reported for the forged order


def test_broker_reject_is_reported_terminal_so_aegis_frees_the_reservation(
    pb2: dict[str, ModuleType],
) -> None:
    from execution_motor.errors import BrokerRejectedError

    transport = InMemoryReportTransport()
    motor = fixture_motor(
        {"alpaca-paper": FakeBroker(outcome=BrokerRejectedError("no funds", http_status=403))}
    )
    report = handle_decision(
        motor,
        decision_from_fixture(pb2, "buy"),
        now_ns=FX_NOW_NS,
        reporter=reporter(pb2, transport),
    )
    assert report.reject_reason is RejectReason.BROKER_REJECTED
    assert transport.messages[0].status == 5  # ORDER_REJECTED
