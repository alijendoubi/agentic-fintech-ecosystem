"""Unit tests for ExecutionMotorServicer: broker and Aegis reporter are both fakes/mocks;
no network, no real grpc transport (that is covered by test_server_integration.py)."""

from __future__ import annotations

from types import ModuleType
from typing import Any

from execution_motor.grpc_service import ExecutionMotorServicer
from execution_motor.halt import KillSwitch
from execution_motor.models import ExecutionStatus

from .helpers import NOW_NS
from .test_motor import FakeBroker, make_motor
from .test_service import decision


class _NullContext:
    """Stand-in for grpc.ServicerContext; unused by ExecutionMotorServicer today."""


def test_execute_approved_decision_reaches_broker_and_reports_to_aegis(
    pb2: dict[str, ModuleType],
) -> None:
    fake_broker = FakeBroker()
    motor = make_motor(fake_broker)
    reports: list[Any] = []

    class _Reporter:
        def report_execution(self, report: Any) -> bool:
            reports.append(report)
            return True

    servicer = ExecutionMotorServicer(
        motor,
        pb2["motor"],
        kill_switch=KillSwitch(start_halted=False),
        reporter=_Reporter(),
        clock_ns=lambda: NOW_NS,
    )
    ack = servicer.Execute(decision(pb2), _NullContext())
    assert ack.accepted is True
    assert ack.status == ExecutionStatus.FILLED.value
    assert ack.reject_reason == ""
    assert len(fake_broker.submitted) == 1
    assert len(reports) == 1  # order reached the broker: reported back to Aegis


def test_execute_tampered_decision_never_reaches_broker(pb2: dict[str, ModuleType]) -> None:
    fake_broker = FakeBroker()
    motor = make_motor(fake_broker)
    servicer = ExecutionMotorServicer(
        motor, pb2["motor"], kill_switch=KillSwitch(start_halted=False), clock_ns=lambda: NOW_NS
    )
    msg = decision(pb2)
    # Corrupt the signature on BOTH copies (attestation and its OrderRequest mirror) so the
    # order/attestation consistency check still passes and the digest/signature check is the
    # one that fails (fail closed: an unverifiable attestation never reaches the broker).
    tampered = b"\x00" * len(msg.attestation.signature)
    msg.attestation.signature = tampered
    msg.order.hsm_signature = tampered
    ack = servicer.Execute(msg, _NullContext())
    assert ack.accepted is False
    assert ack.reject_reason == "attestation_invalid"
    assert fake_broker.submitted == []  # never reached the broker


def test_execute_duplicate_signal_is_rejected_without_resubmitting(
    pb2: dict[str, ModuleType],
) -> None:
    fake_broker = FakeBroker()
    motor = make_motor(fake_broker)
    servicer = ExecutionMotorServicer(
        motor, pb2["motor"], kill_switch=KillSwitch(start_halted=False), clock_ns=lambda: NOW_NS
    )
    msg = decision(pb2)
    first = servicer.Execute(msg, _NullContext())
    second = servicer.Execute(msg, _NullContext())
    assert first.accepted is True
    assert second.accepted is False
    assert second.reject_reason == "duplicate_order"
    assert len(fake_broker.submitted) == 1  # the duplicate never reached the broker


def test_health_reports_halted_state() -> None:
    kill = KillSwitch(start_halted=False)
    motor = make_motor(FakeBroker(), halted=False)

    class _FakeHealthPb2:
        class HealthStatus:
            def __init__(self, *, ok: bool, halted: bool, detail: str) -> None:
                self.ok = ok
                self.halted = halted
                self.detail = detail

    servicer = ExecutionMotorServicer(motor, _FakeHealthPb2, kill_switch=kill)
    healthy = servicer.Health(None, _NullContext())
    assert healthy.ok is True
    assert healthy.halted is False

    kill.halt("test halt")
    unhealthy = servicer.Health(None, _NullContext())
    assert unhealthy.ok is False
    assert unhealthy.halted is True
    assert unhealthy.detail == "test halt"
