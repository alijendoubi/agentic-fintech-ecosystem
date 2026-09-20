"""Defence-in-depth limits: idempotency store and notional caps (Aegis is the primary gate)."""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Protocol

from .models import Order

_ZERO = Decimal(0)


class IdempotencyStore(Protocol):
    def claim(self, key: str) -> bool:
        """Atomically record ``key``. True only for the first caller; False = duplicate.

        A claim is NEVER released, not even after a failure: after an ambiguous submit the
        order may exist at the broker, so re-submission must stay blocked.
        """
        ...


class InMemoryIdempotencyStore:
    """Process-local store. Forgets on restart: a persistent (Redis) store is a follow-up;
    meanwhile the broker's own client_order_id uniqueness is the second line of defence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True


def _usable_price(value: Decimal | None) -> Decimal | None:
    if value is None or not value.is_finite() or value <= _ZERO:
        return None
    return value


def compute_notional(order: Order, reference_price: Decimal | None) -> Decimal | None:
    """Worst-case notional: quantity x the highest usable price among limit, stop, reference.

    Returns None when no usable price exists (e.g. a market order without a reference
    price), which the motor treats as a rejection: an unknown exposure cannot be capped.
    """
    prices = [
        p
        for p in (
            _usable_price(order.limit_price),
            _usable_price(order.stop_price),
            _usable_price(reference_price),
        )
        if p is not None
    ]
    if not prices:
        return None
    return order.quantity * max(prices)


class NotionalLedger:
    """Session-cumulative notional reservation. Thread-safe."""

    def __init__(self, cap: Decimal) -> None:
        if not cap.is_finite() or cap <= _ZERO:
            raise ValueError("cap must be > 0")
        self._cap = cap
        self._reserved = _ZERO
        self._lock = threading.Lock()

    @property
    def reserved(self) -> Decimal:
        with self._lock:
            return self._reserved

    def try_reserve(self, amount: Decimal) -> bool:
        if not amount.is_finite() or amount <= _ZERO:
            return False
        with self._lock:
            if self._reserved + amount > self._cap:
                return False
            self._reserved += amount
            return True

    def release(self, amount: Decimal) -> None:
        with self._lock:
            self._reserved = max(_ZERO, self._reserved - amount)
