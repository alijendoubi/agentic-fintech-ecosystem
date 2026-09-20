"""Venue-toxicity score: pure functions over realised execution observations.

Definition (all quantities Decimal; every input comes from OUR OWN realised fills, nothing is
assumed about a venue that we have not measured):

  markout_bps  (per fill)  = side * (mid_after_horizon - fill_price) / fill_price * 1e4
      side = +1 buy, -1 sell.  Negative = the mid moved against us after the fill
      (adverse selection). ``mid_after_horizon`` is the mid a fixed horizon after the fill;
      the horizon is the caller's choice and must be identical across venues.
  slippage_bps (per fill)  = side * (fill_price - arrival_mid) / arrival_mid * 1e4
      Positive = we paid up (buy above / sell below the arrival mid).

Over the observations in the window, with q = filled quantity as weight:

  M = sum(q_i * markout_i) / sum(q_i)      (size-weighted mean markout, filled obs only)
  S = sum(q_i * slippage_i) / sum(q_i)     (size-weighted mean slippage, filled obs only)
  R = sum(filled_qty) / sum(ordered_qty)   (fill rate, ALL obs, unfilled included)

  a = clip(-M / markout_scale_bps, 0, 1)       adverse-markout component
  s = clip( S / slippage_scale_bps, 0, 1)      fill-quality (cost) component
  f = clip(1 - R, 0, 1)                        fill-shortfall component

  score = w_m * a + w_s * s + w_f * f          in [0, 1], 0 = benign, 1 = maximally toxic

Favourable markout/slippage never offsets other components (it clips to 0, not below).

The scales and weights are POLICY parameters, not measurements. The defaults below are
UNCALIBRATED placeholders; they must be calibrated on our own paper/live fills before
the score gates real capital (see docs/regulatory/ptc-calibration.md for the process).
If there are fewer than ``min_observations`` filled observations the score is None (unknown);
callers decide what unknown means (the router denies by default).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from .models import Side

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_BPS: Final = Decimal(10_000)
_DAY_NS: Final = 86_400_000_000_000


def _sign(side: Side) -> Decimal:
    return _ONE if side is Side.BUY else -_ONE


def _clip01(value: Decimal) -> Decimal:
    return max(_ZERO, min(_ONE, value))


def markout_bps(side: Side, fill_price: Decimal, mid_after_horizon: Decimal) -> Decimal:
    """Signed post-fill price move in bps; negative = adverse to us."""
    return _sign(side) * (mid_after_horizon - fill_price) / fill_price * _BPS


def slippage_bps(side: Side, fill_price: Decimal, arrival_mid: Decimal) -> Decimal:
    """Signed cost vs arrival mid in bps; positive = worse for us."""
    return _sign(side) * (fill_price - arrival_mid) / arrival_mid * _BPS


@dataclass(frozen=True)
class ToxicityParams:
    """Policy parameters. UNCALIBRATED defaults - see module docstring."""

    markout_scale_bps: Decimal = Decimal(5)
    slippage_scale_bps: Decimal = Decimal(10)
    weight_markout: Decimal = Decimal("0.5")
    weight_slippage: Decimal = Decimal("0.3")
    weight_fill_shortfall: Decimal = Decimal("0.2")
    min_observations: int = 30

    def __post_init__(self) -> None:
        if self.markout_scale_bps <= _ZERO or self.slippage_scale_bps <= _ZERO:
            raise ValueError("scales must be positive")
        weights = (self.weight_markout, self.weight_slippage, self.weight_fill_shortfall)
        if any(w < _ZERO for w in weights) or sum(weights, _ZERO) != _ONE:
            raise ValueError("weights must be non-negative and sum to exactly 1")
        if self.min_observations < 1:
            raise ValueError("min_observations must be >= 1")


@dataclass(frozen=True)
class VenueObservation:
    """One of OUR orders at a venue, with the market data needed to score it."""

    timestamp_ns: int
    side: Side
    ordered_qty: Decimal
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    arrival_mid: Decimal
    mid_after_horizon: Decimal | None

    def __post_init__(self) -> None:
        if not (self.ordered_qty.is_finite() and self.ordered_qty > _ZERO):
            raise ValueError("ordered_qty must be > 0")
        if not (self.filled_qty.is_finite() and _ZERO <= self.filled_qty <= self.ordered_qty):
            raise ValueError("filled_qty must be within [0, ordered_qty]")
        if not (self.arrival_mid.is_finite() and self.arrival_mid > _ZERO):
            raise ValueError("arrival_mid must be > 0")
        if self.filled_qty > _ZERO:
            for name, value in (
                ("avg_fill_price", self.avg_fill_price),
                ("mid_after_horizon", self.mid_after_horizon),
            ):
                if value is None or not value.is_finite() or value <= _ZERO:
                    raise ValueError(f"{name} is required (> 0) when filled_qty > 0")

    @property
    def is_filled(self) -> bool:
        return self.filled_qty > _ZERO


@dataclass(frozen=True)
class ToxicityResult:
    score: Decimal
    mean_markout_bps: Decimal
    mean_slippage_bps: Decimal
    fill_rate: Decimal
    n_fills: int


def compute_toxicity(
    observations: Iterable[VenueObservation], params: ToxicityParams
) -> ToxicityResult | None:
    """Score a venue, or None when there is not enough evidence."""
    obs = tuple(observations)
    fills = tuple(o for o in obs if o.is_filled)
    if len(fills) < params.min_observations:
        return None
    total_filled = sum((o.filled_qty for o in fills), _ZERO)
    mean_markout = (
        sum(
            (
                o.filled_qty
                * markout_bps(o.side, _required(o.avg_fill_price), _required(o.mid_after_horizon))
                for o in fills
            ),
            _ZERO,
        )
        / total_filled
    )
    mean_slippage = (
        sum(
            (
                o.filled_qty * slippage_bps(o.side, _required(o.avg_fill_price), o.arrival_mid)
                for o in fills
            ),
            _ZERO,
        )
        / total_filled
    )
    fill_rate = total_filled / sum((o.ordered_qty for o in obs), _ZERO)
    score = (
        params.weight_markout * _clip01(-mean_markout / params.markout_scale_bps)
        + params.weight_slippage * _clip01(mean_slippage / params.slippage_scale_bps)
        + params.weight_fill_shortfall * _clip01(_ONE - fill_rate)
    )
    return ToxicityResult(
        score=score,
        mean_markout_bps=mean_markout,
        mean_slippage_bps=mean_slippage,
        fill_rate=fill_rate,
        n_fills=len(fills),
    )


def _required(value: Decimal | None) -> Decimal:
    if value is None:  # unreachable: enforced by VenueObservation.__post_init__
        raise ValueError("missing price on a filled observation")
    return value


def filter_window(
    observations: Iterable[VenueObservation], *, now_ns: int, window_days: int
) -> tuple[VenueObservation, ...]:
    """Keep observations within [now - window_days, now]. Future-dated ones are dropped."""
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    start = now_ns - window_days * _DAY_NS
    return tuple(o for o in observations if start <= o.timestamp_ns <= now_ns)
