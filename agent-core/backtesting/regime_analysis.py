"""Per-regime performance breakdown.

Attribution rules (documented so reports can cite them):

* A period return (equity[i] -> equity[i+1]) is attributed to the regime label in force at
  the *start* of the period (``regime[i]``): that is the label the position was held under.
* A round-trip trade is attributed to the regime at the time its entry signal was created.
* Every HMM state from ``market_snapshot.proto`` is always reported; states with no
  observations appear with ``n_periods == 0`` and ``insufficient_evidence == True``
  (an absent regime is never silently dropped or reported as a pass). ``REGIME_UNKNOWN`` is
  reported only when it has observations.
* ``insufficient_evidence`` is True when ``n_periods < min_periods``. The threshold is an
  explicit parameter, not a finding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray

from .metrics import FloatArray, MetricsConfig, max_drawdown, ratio_stats
from .models import HMM_STATES, ClosedTrade, RegimeLabel


@dataclass(frozen=True)
class RegimeStats:
    """Performance within one regime; ``None`` = undefined / no observations."""

    regime: str
    n_periods: int
    n_trades: int
    mean_return: float | None
    total_return: float | None
    annualised_volatility: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown: float | None
    hit_rate: float | None
    profit_factor: float | None
    insufficient_evidence: bool

    def to_dict(self) -> dict[str, float | int | str | bool | None]:
        """JSON-safe mapping."""
        return asdict(self)


def _trade_ratios(trades: Sequence[ClosedTrade]) -> tuple[float | None, float | None]:
    if not trades:
        return None, None
    nets = [t.net_pnl for t in trades]
    wins = sum((n for n in nets if n > 0), start=Decimal(0))
    losses = -sum((n for n in nets if n < 0), start=Decimal(0))
    hit = sum(1 for n in nets if n > 0) / len(nets)
    return hit, (None if losses == 0 else float(wins / losses))


def summarise_returns(
    name: str,
    returns: FloatArray,
    trades: Sequence[ClosedTrade],
    cfg: MetricsConfig,
    min_periods: int,
) -> RegimeStats:
    """Summarise one return series (a regime, or ``"ALL"`` for pooled results)."""
    n = int(returns.shape[0])
    hit, pf = _trade_ratios(trades)
    if n == 0:
        return RegimeStats(name, 0, len(trades), None, None, None, None, None, None, hit, pf, True)
    equity = np.concatenate(([1.0], np.cumprod(1.0 + returns)))
    dd, _, _ = max_drawdown(equity, np.arange(equity.shape[0], dtype=np.int64))
    vol, sharpe, sortino = ratio_stats(returns, cfg)
    return RegimeStats(
        name, n, len(trades), float(np.mean(returns)), float(equity[-1] - 1.0),
        vol, sharpe, sortino, dd, hit, pf, n < min_periods,
    )  # fmt: skip


def regime_breakdown(
    returns: FloatArray,
    regimes: NDArray[np.int8],
    trades: Sequence[ClosedTrade],
    cfg: MetricsConfig,
    *,
    min_periods: int,
) -> dict[str, RegimeStats]:
    """Break ``returns`` and ``trades`` down by regime (see module docstring)."""
    if returns.shape != regimes.shape:
        raise ValueError("returns and regimes must be aligned")
    present = {RegimeLabel(int(r)) for r in np.unique(regimes)}
    labels = list(HMM_STATES)
    if RegimeLabel.REGIME_UNKNOWN in present:
        labels.append(RegimeLabel.REGIME_UNKNOWN)
    out: dict[str, RegimeStats] = {}
    for label in labels:
        mask = regimes == int(label)
        subset = [t for t in trades if t.regime is label]
        out[label.name] = summarise_returns(label.name, returns[mask], subset, cfg, min_periods)
    return out
