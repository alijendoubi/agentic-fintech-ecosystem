"""Immutable Decimal portfolio accounting with invariant checks.

Conventions
-----------
* ``realised_pnl`` is *gross* of fees; fees are tracked separately in ``fees_paid``.
* Position average cost excludes fees.
* Equity identity: ``equity = initial_cash + realised + unrealised - fees``.
* Cash conservation: ``cash = initial_cash - sum(signed_notional) - fees``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from types import MappingProxyType

from .models import AccountingInvariantError, ClosedTrade, Fill, RegimeLabel

ZERO = Decimal(0)
#: Average cost is a Decimal quotient (28 significant digits), so the equity identity is
#: checked to this absolute tolerance; cash conservation is exact.
EQUITY_TOLERANCE = Decimal("1e-9")


@dataclass(frozen=True)
class Position:
    """An open position plus the running totals needed to emit a round-trip record."""

    symbol: str
    quantity: Decimal  # signed: > 0 long, < 0 short
    avg_cost: Decimal
    opened_ts_ns: int
    regime: RegimeLabel
    entry_qty: Decimal  # cumulative absolute opening quantity of this round trip
    entry_notional: Decimal
    exit_qty: Decimal
    exit_notional: Decimal
    realised_gross: Decimal
    fees: Decimal


@dataclass(frozen=True)
class PortfolioSnapshot:
    """What a strategy is allowed to see about the portfolio."""

    ts_ns: int
    cash: Decimal
    equity: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    fees_paid: Decimal
    gross_exposure: Decimal
    positions: Mapping[str, Position]

    def quantity(self, symbol: str) -> Decimal:
        """Signed position size (0 when flat)."""
        pos = self.positions.get(symbol)
        return ZERO if pos is None else pos.quantity


def _open_position(fill: Fill, qty: Decimal, fee: Decimal) -> Position:
    signed = qty if fill.signed_quantity > 0 else -qty
    return Position(
        fill.symbol, signed, fill.price, fill.timestamp_ns, fill.regime,
        qty, qty * fill.price, ZERO, ZERO, ZERO, fee,
    )  # fmt: skip


def _trade_from(pos: Position, direction: str, exit_ts_ns: int) -> ClosedTrade:
    return ClosedTrade(
        symbol=pos.symbol,
        direction=direction,
        entry_ts_ns=pos.opened_ts_ns,
        exit_ts_ns=exit_ts_ns,
        quantity=pos.entry_qty,
        avg_entry=pos.entry_notional / pos.entry_qty,
        avg_exit=pos.exit_notional / pos.exit_qty,
        gross_pnl=pos.realised_gross,
        fees=pos.fees,
        regime=pos.regime,
    )


@dataclass(frozen=True)
class Portfolio:
    """Cash, positions and cumulative accounting totals. All methods return new state."""

    initial_cash: Decimal
    cash: Decimal
    positions: Mapping[str, Position]
    realised_pnl: Decimal
    fees_paid: Decimal
    signed_notional: Decimal  # sum of signed (buy +, sell -) traded value
    traded_notional: Decimal  # sum of absolute traded value

    @classmethod
    def new(cls, initial_cash: Decimal) -> Portfolio:
        """A flat portfolio holding only ``initial_cash``."""
        if initial_cash <= 0:
            raise AccountingInvariantError("initial cash must be positive")
        return cls(initial_cash, initial_cash, MappingProxyType({}), ZERO, ZERO, ZERO, ZERO)

    def apply_fill(self, fill: Fill) -> tuple[Portfolio, tuple[ClosedTrade, ...]]:
        """Apply ``fill`` and return the new portfolio plus any round trips it closed."""
        q = fill.signed_quantity
        positions = dict(self.positions)
        closed: tuple[ClosedTrade, ...] = ()
        realised = ZERO
        pos = positions.get(fill.symbol)
        if pos is None or (pos.quantity > 0) == (q > 0):
            positions[fill.symbol] = self._increase(pos, fill)
        else:
            new_pos, realised, closed = self._reduce(pos, fill)
            if new_pos is None:
                del positions[fill.symbol]
            else:
                positions[fill.symbol] = new_pos
        nxt = replace(
            self,
            cash=self.cash - q * fill.price - fill.fee,
            positions=MappingProxyType(positions),
            realised_pnl=self.realised_pnl + realised,
            fees_paid=self.fees_paid + fill.fee,
            signed_notional=self.signed_notional + q * fill.price,
            traded_notional=self.traded_notional + fill.notional,
        )
        return nxt, closed

    @staticmethod
    def _increase(pos: Position | None, fill: Fill) -> Position:
        if pos is None:
            return _open_position(fill, fill.quantity, fill.fee)
        new_abs = abs(pos.quantity) + fill.quantity
        avg = (abs(pos.quantity) * pos.avg_cost + fill.quantity * fill.price) / new_abs
        return replace(
            pos,
            quantity=pos.quantity + fill.signed_quantity,
            avg_cost=avg,
            entry_qty=pos.entry_qty + fill.quantity,
            entry_notional=pos.entry_notional + fill.quantity * fill.price,
            fees=pos.fees + fill.fee,
        )

    @staticmethod
    def _reduce(
        pos: Position, fill: Fill
    ) -> tuple[Position | None, Decimal, tuple[ClosedTrade, ...]]:
        direction = 1 if pos.quantity > 0 else -1
        close_qty = min(fill.quantity, abs(pos.quantity))
        realised = close_qty * (fill.price - pos.avg_cost) * direction
        fee_close = fill.fee * close_qty / fill.quantity
        updated = replace(
            pos,
            quantity=pos.quantity + fill.signed_quantity,
            exit_qty=pos.exit_qty + close_qty,
            exit_notional=pos.exit_notional + close_qty * fill.price,
            realised_gross=pos.realised_gross + realised,
            fees=pos.fees + fee_close,
        )
        if abs(pos.quantity) > close_qty:  # partial reduction, still open
            return updated, realised, ()
        trade = _trade_from(updated, "LONG" if direction > 0 else "SHORT", fill.timestamp_ns)
        leftover = fill.quantity - close_qty
        if leftover == 0:
            return None, realised, (trade,)
        flipped = _open_position(fill, leftover, fill.fee - fee_close)
        return flipped, realised, (trade,)

    def unrealised_pnl(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Mark-to-market P&L of open positions (missing marks fall back to cost)."""
        total = ZERO
        for sym, pos in self.positions.items():
            total += pos.quantity * (marks.get(sym, pos.avg_cost) - pos.avg_cost)
        return total

    def gross_exposure(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Sum of absolute position market values."""
        return sum(
            (abs(p.quantity) * marks.get(s, p.avg_cost) for s, p in self.positions.items()),
            start=ZERO,
        )

    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Cash plus signed market value of all positions."""
        value = sum(
            (p.quantity * marks.get(s, p.avg_cost) for s, p in self.positions.items()),
            start=ZERO,
        )
        return self.cash + value

    def check_invariants(self, marks: Mapping[str, Decimal]) -> None:
        """Raise :class:`AccountingInvariantError` when cash or equity do not reconcile."""
        expected_cash = self.initial_cash - self.signed_notional - self.fees_paid
        if self.cash != expected_cash:
            raise AccountingInvariantError(
                f"cash conservation violated: cash={self.cash} expected={expected_cash}"
            )
        expected_equity = (
            self.initial_cash + self.realised_pnl + self.unrealised_pnl(marks) - self.fees_paid
        )
        if abs(self.equity(marks) - expected_equity) > EQUITY_TOLERANCE:
            raise AccountingInvariantError(
                f"equity identity violated: equity={self.equity(marks)} expected={expected_equity}"
            )

    def snapshot(self, ts_ns: int, marks: Mapping[str, Decimal]) -> PortfolioSnapshot:
        """Frozen view for strategies."""
        return PortfolioSnapshot(
            ts_ns,
            self.cash,
            self.equity(marks),
            self.realised_pnl,
            self.unrealised_pnl(marks),
            self.fees_paid,
            self.gross_exposure(marks),
            self.positions,
        )
