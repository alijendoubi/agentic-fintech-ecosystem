"""Portfolio accounting tests (Decimal arithmetic, invariants). All numbers hand-computed."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from backtesting.models import AccountingInvariantError, Fill, RegimeLabel, Side
from backtesting.portfolio import Portfolio


def fill(side: Side, qty: str, price: str, ts: int, fee: str = "0", oid: int = 1) -> Fill:
    return Fill(oid, "AAA", side, D(qty), D(price), D(price), D(fee), ts, ts - 1)


def test_buy_reduces_cash_and_sets_average_cost() -> None:
    p = Portfolio.new(D("10000"))
    p, closed = p.apply_fill(fill(Side.BUY, "10", "100", 1, fee="1"))
    p, _ = p.apply_fill(fill(Side.BUY, "10", "110", 2, fee="1"))
    assert closed == ()
    assert p.cash == D("10000") - D("1000") - D("1100") - D("2")
    pos = p.positions["AAA"]
    assert pos.quantity == D("20") and pos.avg_cost == D("105")
    assert p.fees_paid == D("2")


def test_realised_and_unrealised_pnl() -> None:
    p = Portfolio.new(D("10000"))
    p, _ = p.apply_fill(fill(Side.BUY, "10", "100", 1))
    p, closed = p.apply_fill(fill(Side.SELL, "4", "120", 2, fee="0.5"))
    assert closed == ()  # still open
    assert p.realised_pnl == D("80")
    marks = {"AAA": D("110")}
    assert p.unrealised_pnl(marks) == D("60")  # 6 * (110-100)
    assert p.equity(marks) == D("10000") + D("80") + D("60") - D("0.5")
    p.check_invariants(marks)


def test_round_trip_ledger_entry_with_fees_and_regime() -> None:
    p = Portfolio.new(D("10000"))
    f1 = Fill(1, "AAA", Side.BUY, D("10"), D("100"), D("100"), D("1"), 10, 9, RegimeLabel.CRISIS)
    p, _ = p.apply_fill(f1)
    p, closed = p.apply_fill(fill(Side.SELL, "10", "110", 20, fee="1", oid=2))
    assert "AAA" not in p.positions
    (trade,) = closed
    assert trade.direction == "LONG" and trade.quantity == D("10")
    assert trade.avg_entry == D("100") and trade.avg_exit == D("110")
    assert trade.gross_pnl == D("100") and trade.fees == D("2") and trade.net_pnl == D("98")
    assert trade.entry_ts_ns == 10 and trade.exit_ts_ns == 20 and trade.holding_ns == 10
    assert trade.regime is RegimeLabel.CRISIS
    assert trade.return_fraction == pytest.approx(0.098)
    assert p.cash == D("10000") + D("100") - D("2")


def test_short_position_pnl() -> None:
    p = Portfolio.new(D("10000"))
    p, _ = p.apply_fill(fill(Side.SELL_SHORT, "10", "100", 1))
    assert p.positions["AAA"].quantity == D("-10")
    assert p.cash == D("11000")
    p, closed = p.apply_fill(fill(Side.BUY, "10", "90", 2, oid=2))
    (trade,) = closed
    assert trade.direction == "SHORT" and trade.gross_pnl == D("100")
    assert p.cash == D("10100")


def test_flip_closes_and_reopens_with_fee_split() -> None:
    p = Portfolio.new(D("10000"))
    p, _ = p.apply_fill(fill(Side.BUY, "10", "100", 1))
    p, closed = p.apply_fill(fill(Side.SELL_SHORT, "15", "90", 2, fee="3", oid=2))
    (trade,) = closed
    assert trade.gross_pnl == D("-100") and trade.fees == D("2")  # 10/15 of the fee
    pos = p.positions["AAA"]
    assert pos.quantity == D("-5") and pos.avg_cost == D("90")
    p.check_invariants({"AAA": D("90")})


def test_equity_identity_holds_over_many_random_fills() -> None:
    import random

    rng = random.Random(7)  # deterministic
    p = Portfolio.new(D("100000"))
    price = D("50")
    for i in range(300):
        price = max(D("1"), price + D(rng.randint(-3, 3)))
        side = rng.choice([Side.BUY, Side.SELL, Side.SELL_SHORT])
        p, _ = p.apply_fill(fill(side, str(rng.randint(1, 20)), str(price), i + 1, "0.01", i))
        p.check_invariants({"AAA": price})


def test_tampered_state_fails_invariant() -> None:
    p = Portfolio.new(D("1000"))
    p, _ = p.apply_fill(fill(Side.BUY, "1", "10", 1))
    from dataclasses import replace

    broken = replace(p, cash=p.cash + D("0.01"))
    with pytest.raises(AccountingInvariantError, match="cash conservation"):
        broken.check_invariants({"AAA": D("10")})


def test_immutability_and_snapshot() -> None:
    p0 = Portfolio.new(D("1000"))
    p1, _ = p0.apply_fill(fill(Side.BUY, "1", "10", 1))
    assert p0.cash == D("1000") and not p0.positions  # original untouched
    with pytest.raises(TypeError):
        p1.positions["AAA"] = p1.positions["AAA"]  # type: ignore[index]
    snap = p1.snapshot(5, {"AAA": D("12")})
    assert snap.ts_ns == 5 and snap.equity == D("1002") and snap.quantity("AAA") == D("1")
    assert snap.quantity("ZZZ") == D("0")
    assert snap.gross_exposure == D("12")
