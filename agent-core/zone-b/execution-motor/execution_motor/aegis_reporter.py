"""Client for ``Aegis.ReportExecution`` (aegis.proto): fills and equity flow back to Aegis.

What Aegis expects (aegis README, "NOTES: relation to the execution-motor"):
* ``filled_qty_nanos`` is CUMULATIVE for the order; a terminal status frees the reservation;
* ``account_equity_nanos`` is on EVERY report (drawdown control C15; the first equity report
  of a UTC day is that day's starting NAV, so report equity at session open);
* a report with an empty ``order_id`` is an equity-only update.

The transport is pluggable. ``GrpcReportTransport`` wraps any generated ``AegisStub`` (duck
typed, so this package does not import grpc); tests and the e2e use ``InMemoryReportTransport``.
A report that cannot be delivered is kept and re-sent by ``flush`` (reports are idempotent
because they are cumulative). A missing equity value is never replaced by zero.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Final, Protocol

import structlog

from .models import ExecutionReport, ExecutionStatus
from .service import to_aegis_execution_report, to_nanos

_log = structlog.get_logger("execution_motor.aegis_reporter")

_TERMINAL: Final = frozenset(
    {
        ExecutionStatus.FILLED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.EXPIRED,
        ExecutionStatus.REJECTED,
    }
)
_DEFAULT_ATTEMPTS: Final = 3
_DEFAULT_TIMEOUT_S: Final = 2.0


class ReportTransport(Protocol):
    def report_execution(self, message: Any) -> bool:
        """Deliver one ``ExecutionReport`` message. True iff Aegis acknowledged (Ack.ok)."""
        ...


class GrpcReportTransport:
    """Adapter over a generated ``AegisStub`` (channel/mTLS creation is the caller's job)."""

    def __init__(self, stub: Any, *, timeout_s: float = _DEFAULT_TIMEOUT_S) -> None:
        self._stub = stub
        self._timeout_s = timeout_s

    def report_execution(self, message: Any) -> bool:
        ack = self._stub.ReportExecution(message, timeout=self._timeout_s)
        return bool(ack.ok)


class InMemoryReportTransport:
    """Records every message; can be told to fail. For tests and local runs, not production."""

    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.fail_next = 0

    def report_execution(self, message: Any) -> bool:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("aegis unreachable")
        self.messages.append(message)
        return True


class AegisReporter:
    def __init__(
        self,
        transport: ReportTransport,
        message_cls: Any,
        *,
        equity_provider: Callable[[], Decimal],
        clock_ns: Callable[[], int] = time.time_ns,
        max_attempts: int = _DEFAULT_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._transport = transport
        self._message_cls = message_cls
        self._equity = equity_provider
        self._clock = clock_ns
        self._attempts = max_attempts
        self._lock = threading.Lock()
        self._sent_qty: dict[str, Decimal] = {}
        self._closed: set[str] = set()
        self._pending: list[ExecutionReport] = []

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)

    def report_execution(self, report: ExecutionReport) -> bool:
        """Send one report (cumulative fills, status, current equity). False = not delivered
        (kept for ``flush``) or refused (would move an order backwards)."""
        if not self._acceptable(report):
            return False
        if self._deliver(report):
            self._remember(report)
            return True
        with self._lock:
            self._pending.append(report)
        return False

    def report_equity(self) -> bool:
        """Equity-only update (empty ``order_id``), e.g. at session open."""
        try:
            equity = self._equity()
            message = self._message_cls(
                account_equity_nanos=to_nanos(equity), reported_at_ns=self._clock()
            )
        except Exception as exc:  # noqa: BLE001 - equity unavailable: report nothing, not zero
            _log.error("equity_unavailable", error=type(exc).__name__)
            return False
        return self._send(message)

    def flush(self) -> int:
        """Re-send undelivered reports in order; returns how many are still pending."""
        with self._lock:
            queue, self._pending = self._pending, []
        for index, report in enumerate(queue):
            if self._deliver(report):
                self._remember(report)
            else:
                with self._lock:
                    self._pending = [*queue[index:], *self._pending]
                break
        return self.pending

    # ---------------------------------------------------------------- internals

    def _acceptable(self, report: ExecutionReport) -> bool:
        with self._lock:
            if report.order_id in self._closed:
                _log.warning("report_after_terminal_dropped", order_id=report.order_id)
                return False
            if report.filled_quantity < self._sent_qty.get(report.order_id, Decimal(0)):
                _log.error("report_would_lower_cumulative_fill", order_id=report.order_id)
                return False
        return True

    def _remember(self, report: ExecutionReport) -> None:
        with self._lock:
            self._sent_qty[report.order_id] = report.filled_quantity
            if report.status in _TERMINAL:
                self._closed.add(report.order_id)

    def _deliver(self, report: ExecutionReport) -> bool:
        try:
            message = to_aegis_execution_report(
                self._message_cls,
                report,
                account_equity=self._equity(),
                reported_at_ns=self._clock(),
            )
        except Exception as exc:  # noqa: BLE001 - no equity / bad value: keep, never send zero
            _log.error("report_not_built", order_id=report.order_id, error=type(exc).__name__)
            return False
        return self._send(message)

    def _send(self, message: Any) -> bool:
        for _ in range(self._attempts):
            try:
                if self._transport.report_execution(message):
                    return True
            except Exception as exc:  # noqa: BLE001 - transport failure: retry, then keep
                _log.warning("report_transport_error", error=type(exc).__name__)
        return False
