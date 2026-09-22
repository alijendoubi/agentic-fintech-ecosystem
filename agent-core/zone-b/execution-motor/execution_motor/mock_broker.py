"""In-memory broker for local, non-production runs. NEVER a production path: the server's
own bootstrap (``server_config.ServerConfig.from_env``) refuses ``MOTOR_USE_MOCK_BROKER=1``
whenever ``MOTOR_ENV`` is production (or unset), the same way ``AegisAttestationVerifier``
refuses a dev signing key in production.

Fills every order immediately and in full: there is no real venue behind it, so this must
never be reachable outside development/paper-less local runs.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Final

from .broker import AccountSnapshot, Broker, BrokerOrder, BrokerOrderRequest, Position
from .models import ExecutionStatus

_PLACEHOLDER_MARKET_PRICE: Final = Decimal("1")


class MockBroker(Broker):
    """Deterministic, in-memory, single-process. Always fills at the order's limit/stop
    price, or a fixed placeholder price for a market order (there is no real quote here)."""

    def __init__(self, *, venue: str = "mock", clock_ns: Callable[[], int] = time.time_ns) -> None:
        self._venue = venue
        self._clock = clock_ns
        self._lock = threading.Lock()
        self._orders: dict[str, BrokerOrder] = {}

    @property
    def venue(self) -> str:
        return self._venue

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrder:
        price = request.limit_price or request.stop_price or _PLACEHOLDER_MARKET_PRICE
        now = self._clock()
        order = BrokerOrder(
            broker_order_id=f"mock-{request.client_order_id}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            status=ExecutionStatus.FILLED,
            raw_status="filled",
            quantity=request.quantity,
            filled_quantity=request.quantity,
            filled_avg_price=price,
            submitted_at_ns=now,
            filled_at_ns=now,
        )
        with self._lock:
            self._orders[request.client_order_id] = order
        return order

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        with self._lock:
            return self._orders.get(client_order_id)

    def cancel_order(self, broker_order_id: str) -> None:
        return None

    def get_positions(self) -> tuple[Position, ...]:
        return ()

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            status="ACTIVE",
            buying_power=Decimal(0),
            cash=Decimal(0),
            equity=Decimal(0),
            trading_blocked=False,
        )
