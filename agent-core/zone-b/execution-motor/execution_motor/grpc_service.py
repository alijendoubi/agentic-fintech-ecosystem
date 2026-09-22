"""gRPC servicer for ``ExecutionMotor.Execute``/``Health`` (execution_motor.proto).

Duck-typed on purpose (no import of the generated ``execution_motor_pb2_grpc`` module):
grpc dispatches an RPC by looking up the method name on whatever object was registered
with ``add_ExecutionMotorServicer_to_server``, so this class does not need to subclass the
generated base. ``pb2`` (the generated ``execution_motor_pb2`` module) is injected so this
module never hard-depends on where the build puts the generated stubs.

This is only the gRPC <-> ``handle_decision`` bridge. It never re-implements or
short-circuits any pipeline step (attestation verification, idempotency, notional caps,
halt checks, broker submission): all of that lives in ``motor.py``/``service.py`` and is
called through unchanged. A non-OK gRPC status is reserved for cases the motor could not
even evaluate (deadline, exception building the response); every policy outcome (rejected,
duplicate, halted, unknown) comes back as an ordinary ``ExecuteAck``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import structlog

from .aegis_reporter import AegisReporter
from .halt import KillSwitch
from .models import ExecutionStatus
from .motor import ExecutionMotor
from .service import handle_decision

_log = structlog.get_logger("execution_motor.grpc_service")

_REACHED_BROKER_ACCEPTED: frozenset[ExecutionStatus] = frozenset(
    {ExecutionStatus.ACCEPTED, ExecutionStatus.PARTIALLY_FILLED, ExecutionStatus.FILLED}
)


class ExecutionMotorServicer:
    """Implements the ``ExecutionMotor`` service (``Execute``, ``Health``)."""

    def __init__(
        self,
        motor: ExecutionMotor,
        pb2: Any,
        *,
        kill_switch: KillSwitch,
        reporter: AegisReporter | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._motor = motor
        self._pb2 = pb2
        self._kill = kill_switch
        self._reporter = reporter
        self._clock = clock_ns

    def Execute(self, request: Any, context: Any) -> Any:  # noqa: N802 - grpc method name
        report = handle_decision(
            self._motor, request, now_ns=self._clock(), reporter=self._reporter
        )
        _log.info(
            "execute_rpc",
            order_id=report.order_id,
            status=report.status.value,
            reason=report.reject_reason.value if report.reject_reason else None,
        )
        return self._pb2.ExecuteAck(
            accepted=report.status in _REACHED_BROKER_ACCEPTED,
            order_id=report.order_id,
            status=report.status.value,
            reject_reason=report.reject_reason.value if report.reject_reason else "",
            detail=report.reject_detail,
        )

    def Health(self, request: Any, context: Any) -> Any:  # noqa: N802 - grpc method name
        halted = self._kill.is_halted()
        return self._pb2.HealthStatus(
            ok=not halted,
            halted=halted,
            detail=self._kill.reason if halted else "",
        )
