"""Reference strategies used for smoke tests and examples.

These are deliberately trivial. They are NOT AFE-STRATEGY-001 (no specification exists in
the repository) and make no claim about profitability.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

from .feed import MarketView
from .models import OrderIntent, Side
from .portfolio import PortfolioSnapshot

StrategyFn = Callable[[MarketView, PortfolioSnapshot], Sequence[OrderIntent]]


def buy_and_hold(symbol: str, fraction: float = 0.95) -> StrategyFn:
    """Buy once with ``fraction`` of equity at the first observable bar, then hold."""

    def strategy(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        latest = view.latest(symbol)
        if latest is None or port.quantity(symbol) != 0:
            return []
        qty = int(Decimal(str(fraction)) * port.equity / Decimal(str(latest.close)))
        return [OrderIntent.of(symbol, Side.BUY, qty)] if qty > 0 else []

    return _once_only(strategy)


def _once_only(inner: StrategyFn) -> StrategyFn:
    state = {"done": False}

    def strategy(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        if state["done"]:
            return []
        intents = inner(view, port)
        state["done"] = bool(intents)
        return intents

    return strategy


def sma_crossover(symbol: str, fast: int = 5, slow: int = 20, fraction: float = 0.95) -> StrategyFn:
    """Long when the fast SMA is above the slow SMA, flat otherwise (long-only)."""
    if not 0 < fast < slow:
        raise ValueError("require 0 < fast < slow")

    def strategy(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        hist = view.history(symbol)
        if len(hist) < slow or hist.latest is None:
            return []
        closes = hist.closes(slow)
        bullish = closes[-fast:].mean() > closes.mean()
        held = port.quantity(symbol)
        if bullish and held == 0:
            qty = int(Decimal(str(fraction)) * port.equity / Decimal(str(hist.latest.close)))
            return [OrderIntent.of(symbol, Side.BUY, qty)] if qty > 0 else []
        if not bullish and held > 0:
            return [OrderIntent.of(symbol, Side.SELL, held)]
        return []

    return strategy


REFERENCE_STRATEGIES: Mapping[str, Callable[[str], StrategyFn]] = {
    "buy_and_hold": buy_and_hold,
    "sma_crossover": sma_crossover,
}
