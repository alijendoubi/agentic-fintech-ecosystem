from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from execution_motor.limits import (
    InMemoryIdempotencyStore,
    NotionalLedger,
    compute_notional,
)
from execution_motor.models import OrderType

from .helpers import make_order


def test_idempotency_claim_is_first_come_only() -> None:
    store = InMemoryIdempotencyStore()
    assert store.claim("a") is True
    assert store.claim("a") is False
    assert store.claim("b") is True


def test_idempotency_claim_is_atomic_under_threads() -> None:
    store = InMemoryIdempotencyStore()
    wins: list[bool] = []

    def worker() -> None:
        wins.append(store.claim("same"))

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1


def test_idempotency_store_evicts_nothing_silently() -> None:
    # Bounded growth is a Redis-store concern; the in-memory store must never forget a key,
    # because forgetting would re-enable a replay.
    store = InMemoryIdempotencyStore()
    for i in range(5000):
        store.claim(f"k{i}")
    assert store.claim("k0") is False


def test_notional_limit_order_uses_limit_price() -> None:
    order = make_order(quantity=Decimal("10"), limit_price=Decimal("190.50"))
    assert compute_notional(order, None) == Decimal("1905.00")


def test_notional_market_order_needs_reference_price() -> None:
    order = make_order(order_type=OrderType.MARKET, limit_price=Decimal("0"))
    assert compute_notional(order, None) is None
    assert compute_notional(order, Decimal("200")) == Decimal("2000")


def test_notional_takes_worst_case_of_available_prices() -> None:
    order = make_order(quantity=Decimal("10"), limit_price=Decimal("100"))
    assert compute_notional(order, Decimal("150")) == Decimal("1500")
    stop = make_order(
        order_type=OrderType.STOP, limit_price=Decimal("0"), stop_price=Decimal("90"),
        quantity=Decimal("2"),
    )
    assert compute_notional(stop, None) == Decimal("180")


@pytest.mark.parametrize("ref", [Decimal("0"), Decimal("-5"), Decimal("NaN")])
def test_notional_bad_reference_price_is_undeterminable(ref: Decimal) -> None:
    order = make_order(order_type=OrderType.MARKET, limit_price=Decimal("0"))
    assert compute_notional(order, ref) is None


def test_ledger_reserves_up_to_cap_and_refuses_beyond() -> None:
    ledger = NotionalLedger(cap=Decimal("1000"))
    assert ledger.try_reserve(Decimal("600")) is True
    assert ledger.try_reserve(Decimal("400")) is True
    assert ledger.try_reserve(Decimal("0.01")) is False
    assert ledger.reserved == Decimal("1000")


def test_ledger_release_frees_capacity() -> None:
    ledger = NotionalLedger(cap=Decimal("1000"))
    ledger.try_reserve(Decimal("1000"))
    ledger.release(Decimal("400"))
    assert ledger.try_reserve(Decimal("400")) is True


def test_ledger_release_never_goes_negative() -> None:
    ledger = NotionalLedger(cap=Decimal("1000"))
    ledger.release(Decimal("5"))
    assert ledger.reserved == Decimal("0")


def test_ledger_refuses_non_positive_reservation() -> None:
    ledger = NotionalLedger(cap=Decimal("1000"))
    assert ledger.try_reserve(Decimal("0")) is False
    assert ledger.try_reserve(Decimal("-1")) is False


def test_ledger_requires_positive_cap() -> None:
    with pytest.raises(ValueError):
        NotionalLedger(cap=Decimal("0"))
