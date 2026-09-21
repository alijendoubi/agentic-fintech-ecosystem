"""The execution motor: attestation-gated, cap-checked, halt-aware, single-shot submission.

Pipeline (every step fails closed; nothing here raises to the caller for a policy outcome):
  halt -> attestation (missing/invalid) -> expiry/future -> algo support -> notional cap ->
  routing -> idempotency claims (signal_id AND order_id) -> session notional reservation ->
  halt (again, immediately before the broker call) -> broker.submit_order (exactly once).

Replay keys: ``signal_id`` is inside the signed attestation text, ``order_id`` is not, so an
attacker who re-labels a signed order with a fresh order_id is still caught by the signal claim.
Claims are never released, even on failure: after an ambiguous submit the order may exist.
An UNKNOWN outcome or any unexpected error after the point of no return halts the motor.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

import structlog

from .attestation import AttestationVerifier, DenyAllVerifier, evaluate_attestation
from .broker import Broker, BrokerOrder, BrokerOrderRequest
from .canonical import SIDE_SELL_SHORT
from .config import MotorConfig
from .errors import BrokerRejectedError, ConfigError, SubmitOutcomeUnknown
from .halt import KillSwitch
from .limits import IdempotencyStore, InMemoryIdempotencyStore, NotionalLedger, compute_notional
from .models import (
    AttestedOrder,
    ExecAlgo,
    ExecutionReport,
    ExecutionStatus,
    Fill,
    Order,
    RejectReason,
)
from .sor import SmartOrderRouter
from .toxicity import VenueObservation

_log = structlog.get_logger("execution_motor.motor")
_ZERO = Decimal(0)
VenueStats = Callable[[], Mapping[str, Sequence[VenueObservation]]]


def _no_stats() -> Mapping[str, Sequence[VenueObservation]]:
    return {}


class ExecutionMotor:
    def __init__(
        self,
        *,
        config: MotorConfig,
        brokers: Mapping[str, Broker],
        router: SmartOrderRouter,
        kill_switch: KillSwitch,
        verifier: AttestationVerifier | None = None,
        idempotency: IdempotencyStore | None = None,
        venue_stats: VenueStats = _no_stats,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not brokers:
            raise ConfigError("at least one broker/venue is required")
        self._config = config
        self._brokers = dict(brokers)
        self._router = router
        self._kill = kill_switch
        self._verifier: AttestationVerifier = verifier or DenyAllVerifier()
        if config.is_production and getattr(self._verifier, "accepts_dev_keys", False) is True:
            raise ConfigError("a verifier that accepts dev signing keys is forbidden in production")
        self._idem: IdempotencyStore = idempotency or InMemoryIdempotencyStore()
        self._stats = venue_stats
        self._clock = clock_ns
        self._ledger = NotionalLedger(config.max_session_notional)

    # ------------------------------------------------------------------ public

    @property
    def allow_short_selling(self) -> bool:
        return self._config.allow_short_selling

    def execute(
        self, attested: AttestedOrder, *, reference_price: Decimal | None = None
    ) -> ExecutionReport:
        """Execute one attested order. Always returns a report; never raises."""
        received = self._clock()
        order = attested.order
        try:
            report = self._run(attested, reference_price, received)
        except Exception as exc:  # noqa: BLE001 - last-resort boundary: halt and fail closed
            self._kill.halt(f"unexpected error: {type(exc).__name__}")
            _log.error("motor_internal_error", order_id=order.order_id, error=type(exc).__name__)
            report = self._rejected(
                order, RejectReason.INTERNAL_ERROR, type(exc).__name__, received, reference_price
            )
        _log.info(
            "execution_report",
            order_id=order.order_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            status=report.status.value,
            reason=report.reject_reason.value if report.reject_reason else None,
            venue=report.venue,
        )
        return report

    # ---------------------------------------------------------------- pipeline

    def _run(self, attested: AttestedOrder, ref: Decimal | None, received: int) -> ExecutionReport:
        order = attested.order
        pre = self._precheck(attested, ref, received)
        if isinstance(pre, ExecutionReport):
            return pre
        notional = pre
        decision = self._router.route(order, sorted(self._brokers), self._stats(), now_ns=received)
        if decision.venue is None:
            return self._rejected(order, RejectReason.NO_ELIGIBLE_VENUE, "", received, ref)
        gate = self._claim_and_reserve(order, notional, received, ref)
        if gate is not None:
            return gate
        return self._submit(order, decision.venue, notional, received, ref)

    def _precheck(
        self, attested: AttestedOrder, ref: Decimal | None, received: int
    ) -> ExecutionReport | Decimal:
        order = attested.order
        if self._kill.is_halted():
            return self._rejected(order, RejectReason.HALTED, self._kill.reason, received, ref)
        denial = evaluate_attestation(self._verifier, attested)
        if denial is not None:
            return self._rejected(order, denial, "", received, ref)
        if attested.attestation.attested_side == SIDE_SELL_SHORT and not self.allow_short_selling:
            return self._rejected(order, RejectReason.SHORT_NOT_PERMITTED, "", received, ref)
        timing = self._timing_reason(attested, received)
        if timing is not None:
            return self._rejected(order, timing, "", received, ref)
        if order.algo is not ExecAlgo.DIRECT:
            detail = f"{order.algo.value} not implemented"
            return self._rejected(order, RejectReason.ALGO_UNSUPPORTED, detail, received, ref)
        notional = compute_notional(order, ref)
        if notional is None:
            return self._rejected(order, RejectReason.NOTIONAL_UNDETERMINABLE, "", received, ref)
        if notional > self._config.max_order_notional:
            return self._rejected(order, RejectReason.NOTIONAL_CAP_EXCEEDED, "", received, ref)
        return notional

    def _timing_reason(self, attested: AttestedOrder, now: int) -> RejectReason | None:
        att = attested.attestation
        if att.decided_at_ns > now + self._config.max_clock_skew_ns:
            return RejectReason.ORDER_FROM_FUTURE
        if now >= att.expires_at_ns or now - att.decided_at_ns > self._config.max_order_age_ns:
            return RejectReason.ORDER_EXPIRED
        return None

    def _claim_and_reserve(
        self, order: Order, notional: Decimal, received: int, ref: Decimal | None
    ) -> ExecutionReport | None:
        signal_new = self._idem.claim(f"signal:{order.signal_id}")
        order_new = self._idem.claim(f"order:{order.order_id}")
        if not (signal_new and order_new):
            return self._rejected(order, RejectReason.DUPLICATE_ORDER, "", received, ref)
        if not self._ledger.try_reserve(notional):
            return self._rejected(
                order, RejectReason.SESSION_NOTIONAL_CAP_EXCEEDED, "", received, ref
            )
        return None

    def _submit(
        self, order: Order, venue: str, notional: Decimal, received: int, ref: Decimal | None
    ) -> ExecutionReport:
        if self._kill.is_halted():  # checked immediately before every submit
            self._ledger.release(notional)
            return self._rejected(
                order, RejectReason.HALTED, self._kill.reason, received, ref, venue
            )
        request = BrokerOrderRequest(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            limit_price=order.limit_price or None,
            stop_price=order.stop_price or None,
        )
        submitted = self._clock()
        try:
            result = self._brokers[venue].submit_order(request)
        except BrokerRejectedError as exc:
            self._ledger.release(notional)
            return self._rejected(
                order, RejectReason.BROKER_REJECTED, str(exc), received, ref, venue
            )
        except SubmitOutcomeUnknown as exc:
            return self._unknown(order, venue, str(exc), received, submitted, ref)
        except Exception as exc:  # noqa: BLE001 - after the point of no return: assume it may exist
            return self._unknown(order, venue, type(exc).__name__, received, submitted, ref)
        return self._from_broker(order, venue, result, notional, received, submitted, ref)

    # ---------------------------------------------------------------- reports

    def _from_broker(
        self,
        order: Order,
        venue: str,
        result: BrokerOrder,
        notional: Decimal,
        received: int,
        submitted: int,
        ref: Decimal | None,
    ) -> ExecutionReport:
        if result.status is ExecutionStatus.UNKNOWN:
            return self._unknown(
                order,
                venue,
                f"unmapped broker status {result.raw_status}",
                received,
                submitted,
                ref,
            )
        if result.status is ExecutionStatus.REJECTED:
            self._ledger.release(notional)
            return self._rejected(
                order, RejectReason.BROKER_REJECTED, result.raw_status, received, ref, venue
            )
        now = self._clock()
        fills: tuple[Fill, ...] = ()
        if result.filled_quantity > _ZERO and result.filled_avg_price is not None:
            fills = (
                Fill(
                    quantity=result.filled_quantity,
                    price=result.filled_avg_price,
                    timestamp_ns=result.filled_at_ns or now,
                    venue=venue,
                ),
            )
        return ExecutionReport(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            side=order.side,
            status=result.status,
            requested_quantity=order.quantity,
            broker_order_id=result.broker_order_id,
            venue=venue,
            fills=fills,
            arrival_price=ref,
            received_at_ns=received,
            submitted_at_ns=result.submitted_at_ns or submitted,
            updated_at_ns=now,
        )

    def _unknown(
        self,
        order: Order,
        venue: str,
        detail: str,
        received: int,
        submitted: int,
        ref: Decimal | None,
    ) -> ExecutionReport:
        self._kill.halt(f"submit outcome unknown for order {order.order_id}")
        return ExecutionReport(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            side=order.side,
            status=ExecutionStatus.UNKNOWN,
            requested_quantity=order.quantity,
            reject_reason=RejectReason.SUBMIT_OUTCOME_UNKNOWN,
            reject_detail=detail[:300],
            venue=venue,
            arrival_price=ref,
            received_at_ns=received,
            submitted_at_ns=submitted,
            updated_at_ns=self._clock(),
        )

    def _rejected(
        self,
        order: Order,
        reason: RejectReason,
        detail: str,
        received: int,
        ref: Decimal | None,
        venue: str | None = None,
    ) -> ExecutionReport:
        return ExecutionReport(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            signal_id=order.signal_id,
            symbol=order.symbol,
            side=order.side,
            status=ExecutionStatus.REJECTED,
            requested_quantity=order.quantity,
            reject_reason=reason,
            reject_detail=detail[:300],
            venue=venue,
            arrival_price=ref,
            received_at_ns=received,
            updated_at_ns=self._clock(),
        )
