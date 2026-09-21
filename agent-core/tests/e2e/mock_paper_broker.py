"""In-memory paper broker for the e2e. Implements the motor's ``Broker`` ABC; never networked."""

from __future__ import annotations

from decimal import Decimal

from execution_motor.broker import (
    AccountSnapshot,
    Broker,
    BrokerOrder,
    BrokerOrderRequest,
    Position,
)
from execution_motor.models import ExecutionStatus


class MockPaperBroker(Broker):
    """Fills every accepted order at its limit price (all, or ``fill_fraction`` of it).

    It enforces broker-side idempotency on client_order_id like a real broker would: a second
    submit with the same id is a hard error, so a motor bug would surface in the e2e.
    """

    def __init__(
        self, *, equity: Decimal = Decimal("100000"), fill_fraction: Decimal = Decimal(1)
    ) -> None:
        self._equity = equity
        self._fill_fraction = fill_fraction
        self.submitted: list[BrokerOrderRequest] = []
        self._orders: dict[str, BrokerOrder] = {}

    @property
    def venue(self) -> str:
        return "mock-paper"

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        if request.client_order_id in self._orders:
            raise AssertionError(f"duplicate client_order_id at broker: {request.client_order_id}")
        self.submitted.append(request)
        price = request.limit_price or Decimal(0)
        filled = request.quantity * self._fill_fraction
        status = (
            ExecutionStatus.FILLED
            if filled == request.quantity
            else ExecutionStatus.PARTIALLY_FILLED
        )
        order = BrokerOrder(
            broker_order_id=f"mock-{len(self._orders) + 1}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=status,
            raw_status=status.value,
            quantity=request.quantity,
            filled_quantity=filled,
            filled_avg_price=price if filled > 0 else None,
            submitted_at_ns=1,
            filled_at_ns=2,
        )
        self._orders[request.client_order_id] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._orders.get(client_order_id)

    def cancel_order(self, broker_order_id: str) -> None:
        return None

    def get_positions(self) -> tuple[Position, ...]:
        return ()

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            status="ACTIVE",
            buying_power=self._equity,
            cash=self._equity,
            equity=self._equity,
            trading_blocked=False,
        )
