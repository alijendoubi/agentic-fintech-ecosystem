"""Execution simulation: costs, slippage, participation-capped partial fills, collar.

Conventions
-----------
* Reference price of a fill is the *fill bar's* close (default, ``FillPrice.CLOSE``: the price
  observed at the first event at/after order arrival, so it can never predate the order) or
  its open (``FillPrice.OPEN``: the usual "next-bar open" convention; note the open of a
  close-stamped bar may precede order arrival when latency exceeds the bar spacing).
* Buys pay ``ref * (1 + half_spread + impact)``; sells receive ``ref * (1 - half_spread -
  impact)``.
* Linear impact: ``coef * (qty / bar_volume)``.
  Square-root impact: ``coef * sigma * sqrt(qty / bar_volume)`` where ``sigma`` is the
  standard deviation of the last ``vol_lookback`` log returns up to and including the fill
  bar (falling back to the fill bar's ``(high - low) / close`` range when fewer than two
  returns exist). Coefficients are *inputs*: they must be fitted from data, not assumed.
* The price collar (``collar_bps``) is measured against the decision price (close of the
  bar on which the intent was created). A violation rejects the order (terminal), mirroring
  a hard block.
* Limit orders wait (stay open) until the *executable* price satisfies the limit or the
  order expires.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from .models import Bar, ConfigError, Fill, OrderIntent, RegimeLabel, Rejection, Side

BPS = 1e-4
PRICE_PLACES = 6


class ImpactModel(StrEnum):
    """Market-impact functional form."""

    NONE = "none"
    LINEAR = "linear"
    SQRT = "sqrt"


class FillPrice(StrEnum):
    """Which price of the fill bar is the reference for a fill."""

    OPEN = "open"
    CLOSE = "close"


@dataclass(frozen=True)
class CostModel:
    """Transaction-cost parameters. Money-like fields are Decimal."""

    commission_per_share: Decimal = Decimal(0)
    commission_rate: Decimal = Decimal(0)  # fraction of notional, e.g. 0.0005 = 5 bps
    min_commission: Decimal = Decimal(0)  # per fill
    half_spread_bps: float = 0.0
    impact_model: ImpactModel = ImpactModel.NONE
    impact_coefficient: float = 0.0
    vol_lookback: int = 20

    def __post_init__(self) -> None:
        if min(self.commission_per_share, self.commission_rate, self.min_commission) < 0:
            raise ConfigError("commissions must be non-negative")
        if not math.isfinite(self.half_spread_bps) or self.half_spread_bps < 0:
            raise ConfigError("half_spread_bps must be finite and non-negative")
        if not math.isfinite(self.impact_coefficient) or self.impact_coefficient < 0:
            raise ConfigError("impact_coefficient must be finite and non-negative")
        if self.vol_lookback < 2:
            raise ConfigError("vol_lookback must be >= 2")


@dataclass(frozen=True)
class ExecutionConfig:
    """Latency, capacity and order-handling rules."""

    latency_ns: int = 0
    latency_jitter_ns: int = 0  # uniform [0, jitter] added per order (seeded)
    participation_cap: float = 1.0  # max fraction of a bar's volume filled per symbol
    collar_bps: float | None = None
    fill_price: FillPrice = FillPrice.CLOSE
    default_ttl_ns: int | None = None  # None = good-till-end-of-data
    allow_short: bool = False
    enforce_cash: bool = True
    omega_threshold: float | None = None  # intents with omega below it abstain

    def __post_init__(self) -> None:
        if self.latency_ns < 0 or self.latency_jitter_ns < 0:
            raise ConfigError("latency must be non-negative")
        if not 0.0 < self.participation_cap <= 1.0:
            raise ConfigError("participation_cap must be in (0, 1]")
        if self.collar_bps is not None and self.collar_bps <= 0:
            raise ConfigError("collar_bps must be positive when given")
        if self.default_ttl_ns is not None and self.default_ttl_ns <= 0:
            raise ConfigError("default_ttl_ns must be positive when given")
        if self.omega_threshold is not None and not 0.0 <= self.omega_threshold <= 1.0:
            raise ConfigError("omega_threshold must be within [0, 1]")


@dataclass(frozen=True)
class PendingOrder:
    """An accepted intent waiting for an eligible event."""

    order_id: int
    intent: OrderIntent
    created_ts_ns: int
    eligible_ts_ns: int
    expires_ts_ns: int | None
    decision_price: float
    remaining: Decimal
    regime: RegimeLabel


@dataclass(frozen=True)
class FillAttempt:
    """Outcome of trying to execute an order on one bar."""

    fill: Fill | None
    order: PendingOrder | None  # what stays open (None = order finished)
    rejection: Rejection | None


def to_decimal(value: float) -> Decimal:
    """Deterministically convert a float price to Decimal at :data:`PRICE_PLACES`."""
    return Decimal(f"{value:.{PRICE_PLACES}f}")


def reference_price(bar: Bar, mode: FillPrice) -> float:
    """Reference price of ``bar`` under ``mode``."""
    return bar.open if mode is FillPrice.OPEN else bar.close


def bar_sigma(closes: NDArray[np.float64], bar: Bar) -> float:
    """Volatility proxy for square-root impact (see module docstring)."""
    if closes.shape[0] >= 3:
        return float(np.std(np.diff(np.log(closes)), ddof=1))
    return (bar.high - bar.low) / bar.close


def slippage_fraction(cost: CostModel, quantity: float, volume: float, sigma: float) -> float:
    """Total adverse price fraction (half-spread + impact) for one fill."""
    frac = cost.half_spread_bps * BPS
    participation = quantity / volume if volume > 0 else 0.0
    if cost.impact_model is ImpactModel.LINEAR:
        frac += cost.impact_coefficient * participation
    elif cost.impact_model is ImpactModel.SQRT:
        frac += cost.impact_coefficient * sigma * math.sqrt(participation)
    return frac


def commission(cost: CostModel, quantity: Decimal, notional: Decimal) -> Decimal:
    """Commission for one fill: per-share plus notional rate, floored at the minimum."""
    fee = quantity * cost.commission_per_share + notional * cost.commission_rate
    return max(fee, cost.min_commission)


def _reject(order: PendingOrder, ts: int, reason: str) -> FillAttempt:
    rej = Rejection(order.order_id, order.intent.symbol, ts, reason, order.remaining)
    return FillAttempt(None, None, rej)


def _validate_side(
    order: PendingOrder, position: Decimal, cfg: ExecutionConfig, ts: int
) -> tuple[Decimal, FillAttempt | None]:
    """Return ``(max quantity allowed by inventory rules, terminal rejection or None)``."""
    side = order.intent.side
    if side is Side.SELL:
        if position <= 0:
            return order.remaining, _reject(order, ts, "no_long_position")
        return min(order.remaining, position), None
    if side is Side.SELL_SHORT:
        if not cfg.allow_short:
            return order.remaining, _reject(order, ts, "shorting_disabled")
        if position > 0:
            return order.remaining, _reject(order, ts, "short_while_long")
    return order.remaining, None


def _limit_ok(intent: OrderIntent, price: Decimal) -> bool:
    if intent.price_limit is None:
        return True
    return price <= intent.price_limit if intent.side is Side.BUY else price >= intent.price_limit


def attempt_fill(
    order: PendingOrder,
    bar: Bar,
    closes: NDArray[np.float64],
    capacity_left: Decimal,
    position: Decimal,
    cash: Decimal,
    cost: CostModel,
    cfg: ExecutionConfig,
) -> FillAttempt:
    """Try to (partially) fill ``order`` against ``bar``. Pure: returns new order state."""
    max_qty, terminal = _validate_side(order, position, cfg, bar.timestamp_ns)
    if terminal is not None:
        return terminal
    ref = reference_price(bar, cfg.fill_price)
    if cfg.collar_bps is not None:
        deviation = abs(ref / order.decision_price - 1.0)
        if deviation > cfg.collar_bps * BPS:
            return _reject(order, bar.timestamp_ns, "price_collar")
    qty = min(max_qty, capacity_left)
    if qty <= 0:
        return FillAttempt(None, order, None)  # no capacity on this bar: keep waiting
    sign = 1.0 if order.intent.side is Side.BUY else -1.0
    slip = slippage_fraction(cost, float(qty), bar.volume, bar_sigma(closes, bar))
    price = to_decimal(max(ref * (1.0 + sign * slip), 1e-6))
    if not _limit_ok(order.intent, price):
        return FillAttempt(None, order, None)
    notional = qty * price
    fee = commission(cost, qty, notional)
    if (
        cfg.enforce_cash
        and order.intent.side is Side.BUY
        and position >= 0
        and cash < notional + fee
    ):
        return _reject(order, bar.timestamp_ns, "insufficient_cash")
    fill = Fill(
        order.order_id, order.intent.symbol, order.intent.side, qty, price,
        to_decimal(ref), fee, bar.timestamp_ns, order.created_ts_ns, order.regime,
        order.intent.tag,
    )  # fmt: skip
    left = order.remaining - qty
    return FillAttempt(fill, replace(order, remaining=left) if left > 0 else None, None)
