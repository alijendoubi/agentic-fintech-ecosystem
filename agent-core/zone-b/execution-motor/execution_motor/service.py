"""Glue between generated afe.shared messages and the motor (duck-typed, no stub imports).

``handle_decision`` is what a gRPC servicer would call with an ``AegisDecision``;
``to_aegis_execution_report`` builds the ``ExecutionReport`` message for ``Aegis.ReportExecution``.
The gRPC server/client wiring itself is NOT part of this package yet (see report).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Final, Protocol

from .compliance import DISABLED, ComplianceGates, bind_context
from .errors import OrderValidationError
from .models import AttestedOrder, ExecutionReport, ExecutionStatus, RejectReason, Side
from .motor import ExecutionMotor
from .proto_adapter import attested_order_from_proto

_DECISION_APPROVED: Final = 1  # aegis.proto DecisionStatus
_NANOS: Final = Decimal(10) ** 9
_SIDE_ENUM: Final = {Side.BUY: 1, Side.SELL: 2}  # order_request.proto OrderSide
# order_request.proto OrderStatus. ACCEPTED -> SENT; EXPIRED -> CANCELLED; UNKNOWN -> SENT
# (the order may exist, so it must not be reported as terminal).
_STATUS_ENUM: Final = {
    ExecutionStatus.ACCEPTED: 1,
    ExecutionStatus.PARTIALLY_FILLED: 2,
    ExecutionStatus.FILLED: 3,
    ExecutionStatus.CANCELLED: 4,
    ExecutionStatus.EXPIRED: 4,
    ExecutionStatus.REJECTED: 5,
    ExecutionStatus.UNKNOWN: 1,
}


def to_nanos(value: Decimal) -> int:
    """Decimal -> int64 nanos, half-even at the 9th decimal. Refuses non-finite values."""
    if not value.is_finite():
        raise ValueError("value must be finite")
    return int((value * _NANOS).to_integral_value(rounding=ROUND_HALF_EVEN))


def _invalid_report(
    decision: Any,
    detail: str,
    now_ns: int,
    reason: RejectReason = RejectReason.INVALID_ORDER,
) -> ExecutionReport:
    order = decision.order
    return ExecutionReport(
        order_id=order.order_id or "unknown",
        client_order_id=order.order_id or "unknown",
        signal_id=decision.signal_id or order.signal_id or "unknown",
        symbol=order.symbol or "unknown",
        status=ExecutionStatus.REJECTED,
        requested_quantity=Decimal(0),
        reject_reason=reason,
        reject_detail=detail[:300],
        received_at_ns=now_ns,
        updated_at_ns=now_ns,
    )


class ExecutionReporter(Protocol):
    def report_execution(self, report: ExecutionReport) -> bool: ...


def reached_broker(report: ExecutionReport) -> bool:
    """Only orders that reached the broker are reported to Aegis: a pre-broker rejection (a
    forged, expired or replayed order) reported under a real order_id could free Aegis's
    exposure reservation for an order that is still legitimately live."""
    if report.status is not ExecutionStatus.REJECTED:
        return True
    return report.reject_reason is RejectReason.BROKER_REJECTED


def _attested(motor: ExecutionMotor, decision: Any, now_ns: int) -> AttestedOrder | ExecutionReport:
    """A complete APPROVED decision as an ``AttestedOrder``, else the refusal report."""
    if int(decision.decision) != _DECISION_APPROVED:
        return _invalid_report(decision, "decision is not APPROVED", now_ns)
    if not decision.HasField("order") or not decision.HasField("attestation"):
        return _invalid_report(decision, "APPROVED decision lacks order or attestation", now_ns)
    try:
        return attested_order_from_proto(
            decision.order, decision.attestation, allow_short=motor.allow_short_selling
        )
    except OrderValidationError as exc:
        return _invalid_report(decision, str(exc), now_ns)


def handle_decision(
    motor: ExecutionMotor,
    decision: Any,
    *,
    now_ns: int,
    reference_price: Decimal | None = None,
    reporter: ExecutionReporter | None = None,
    compliance: ComplianceGates = DISABLED,
) -> ExecutionReport:
    """Execute an AegisDecision. Anything other than a complete APPROVED decision is refused.

    ``reference_price`` is unsigned caller data: it is only recorded as the report's
    arrival_price for slippage. The notional cap never uses it (see ``ExecutionMotor``).

    With a ``reporter``, the outcome is sent back to Aegis (``ReportExecution``) when the order
    reached the broker. Delivery problems never change the returned report.

    When ``compliance`` needs a context (always in production), a bare decision is refused:
    use ``handle_request`` (``ExecuteWithContext``).
    """
    if compliance.requires_context:
        return _invalid_report(
            decision,
            "compliance is enforced: use ExecuteWithContext",
            now_ns,
            RejectReason.CONTEXT_MISSING,
        )
    attested = _attested(motor, decision, now_ns)
    if isinstance(attested, ExecutionReport):
        return attested
    return _execute(motor, attested, reference_price, reporter, None)


def handle_request(
    motor: ExecutionMotor,
    request: Any,
    *,
    now_ns: int,
    compliance: ComplianceGates,
    reference_price: Decimal | None = None,
    reporter: ExecutionReporter | None = None,
) -> ExecutionReport:
    """Execute an ``ExecuteRequest`` (decision + context), ALI-161.

    Order: decision checks -> context bound to the signed order (signal_id, symbol and the
    attested strategy_id) -> SHARP gate on the attested strategy_id (before any
    idempotency claim) -> motor pipeline, whose last step before the broker stores the
    Compliance Manifest. A refusal before the motor is never reported to Aegis.
    """
    decision = request.decision
    attested = _attested(motor, decision, now_ns)
    if isinstance(attested, ExecutionReport):
        return attested
    if not request.HasField("context"):
        return _invalid_report(
            decision, "request has no context", now_ns, RejectReason.CONTEXT_MISSING
        )
    context = request.context
    refusal = bind_context(decision, context)
    if refusal is not None:
        return _invalid_report(decision, refusal.detail, now_ns, refusal.reason)
    blocked = compliance.check_strategy(attested.attestation.strategy_id)
    if blocked is not None:
        return _invalid_report(decision, blocked, now_ns, RejectReason.STRATEGY_NOT_PROMOTED)
    check = compliance.release_check(decision, context)
    return _execute(motor, attested, reference_price, reporter, check)


def _execute(
    motor: ExecutionMotor,
    attested: AttestedOrder,
    reference_price: Decimal | None,
    reporter: ExecutionReporter | None,
    release_check: Callable[[], str | None] | None,
) -> ExecutionReport:
    report = motor.execute(attested, reference_price=reference_price, release_check=release_check)
    if reporter is not None and reached_broker(report):
        reporter.report_execution(report)
    return report


def to_aegis_execution_report(
    message_cls: Any, report: ExecutionReport, *, account_equity: Decimal, reported_at_ns: int
) -> Any:
    """Build an aegis.proto ExecutionReport. ``account_equity`` is mandatory: Aegis uses it for
    the drawdown control, so a silent zero would be dangerous."""
    avg = report.avg_fill_price
    return message_cls(
        order_id=report.order_id,
        signal_id=report.signal_id,
        symbol=report.symbol,
        side=_SIDE_ENUM.get(report.side, 0) if report.side else 0,
        status=_STATUS_ENUM[report.status],
        filled_qty_nanos=to_nanos(report.filled_quantity),
        avg_fill_price_nanos=to_nanos(avg) if avg is not None else 0,
        account_equity_nanos=to_nanos(account_equity),
        reported_at_ns=reported_at_ns,
    )
