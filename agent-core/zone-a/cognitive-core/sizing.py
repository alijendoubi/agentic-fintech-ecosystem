"""Position sizing (ALI-160). Runs after the Judge: size depends on its confidence (omega).

Two policies, chosen by the owner (`COGNITIVE_SIZER`, see `runner_config.py`):

* ``fixed`` — the pre-existing fixed `COGNITIVE_ORDER_QUANTITY` (placeholder, kept for dev;
  no sizer object, the runner passes the quantity straight through).
* ``vol_scaled`` — owner-approved policy (session decision 2026-09-29)::

      shares = floor( equity * risk_per_trade * omega / (price * realized_vol) )
      shares = min(shares, floor(max_order_notional / price))

  i.e. the position whose 1-sigma move (at the snapshot's realized volatility) costs
  ``equity * risk_per_trade`` dollars, scaled down by the Judge's confidence and capped by a
  per-order notional. ``realized_vol`` is the snapshot's ``realized_volatility`` as a fraction
  (0.2 = 20 %), in whatever horizon sensory-array computes it; the owner sets
  ``risk_per_trade`` against that same horizon. TODO(owner): confirm the horizon.

Every parameter is owner-supplied with no default; nothing here is calibrated. Arithmetic is
``Decimal`` (no floats for money, Phase 3 spec section 3). Any non-positive or non-finite input
sizes to 0 shares, which makes the signal abstain (``signals.abstain_reasons``): fail closed.
Aegis's order-size and exposure controls and execution-motor's notional caps still apply.

Known limit: ``equity`` is a configured figure, not the live account value (Zone A holds no
broker credentials and Aegis does not expose equity). TODO(owner): feed live equity once a
Zone B read path exists; until then keep it at or below the real account value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Protocol

_ZERO = Decimal(0)
MAX_RISK_PER_TRADE = Decimal("0.05")  # hard ceiling on the configured fraction, not a default


class PositionSizer(Protocol):
    def size(self, *, price: float, realized_vol: float, omega: float) -> float:
        """Share quantity for one signal; 0 means abstain. Must not raise."""
        ...


def _decimal(value: float) -> Decimal | None:
    if not math.isfinite(value):
        return None
    try:
        return Decimal(repr(value))
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class VolScaledSizer:
    equity_usd: Decimal
    risk_per_trade: Decimal
    max_order_notional_usd: Decimal

    def __post_init__(self) -> None:
        if not (self.equity_usd.is_finite() and self.equity_usd > _ZERO):
            raise ValueError("equity_usd must be a positive finite amount")
        if not (
            self.risk_per_trade.is_finite() and _ZERO < self.risk_per_trade <= MAX_RISK_PER_TRADE
        ):
            raise ValueError(f"risk_per_trade must be in (0, {MAX_RISK_PER_TRADE}]")
        if not (self.max_order_notional_usd.is_finite() and self.max_order_notional_usd > _ZERO):
            raise ValueError("max_order_notional_usd must be a positive finite amount")

    def shares(self, *, price: float, realized_vol: float, omega: float) -> int:
        p, vol, conf = _decimal(price), _decimal(realized_vol), _decimal(omega)
        if p is None or vol is None or conf is None:
            return 0
        if p <= _ZERO or vol <= _ZERO or not _ZERO < conf <= Decimal(1):
            return 0
        budget = self.equity_usd * self.risk_per_trade * conf
        by_risk = (budget / (p * vol)).to_integral_value(rounding=ROUND_FLOOR)
        by_cap = (self.max_order_notional_usd / p).to_integral_value(rounding=ROUND_FLOOR)
        return max(0, int(min(by_risk, by_cap)))

    def size(self, *, price: float, realized_vol: float, omega: float) -> float:
        return float(self.shares(price=price, realized_vol=realized_vol, omega=omega))
