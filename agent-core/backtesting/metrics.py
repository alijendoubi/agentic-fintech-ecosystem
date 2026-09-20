"""Performance metrics computed from an equity curve and a trade ledger.

Conventions (all documented here so reports can cite them):

* Period return: simple return of equity between consecutive curve points.
* Annualisation is driven by ``MetricsConfig.periods_per_year`` (e.g. 252 for daily bars,
  ``252 * 390`` for 1-minute regular-hours bars). It is never inferred.
* Sharpe = mean(excess period return) / sample std (ddof=1) * sqrt(periods_per_year), where
  excess = return - risk_free_rate / periods_per_year. Undefined (``None``) when the
  standard deviation is zero or fewer than two returns exist.
* Sortino uses the downside deviation sqrt(mean(min(0, excess)^2)) over all periods.
* Annualised return = (1 + total_return) ** (periods_per_year / n_periods) - 1.
* Max drawdown is a positive fraction of the running peak. Duration is measured from the
  peak to the recovery point (or to the last point when never recovered).
* Hit rate / profit factor use *net* (after-fee) round-trip P&L. Profit factor is
  gross wins / gross losses and is ``None`` when there are no losing trades.
* Turnover = total traded notional / mean equity. Exposure = share of curve points with
  non-zero gross exposure.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray

from .models import ClosedTrade, ConfigError

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class MetricsConfig:
    """Annualisation parameters."""

    periods_per_year: float = 252.0
    risk_free_rate: float = 0.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.periods_per_year) or self.periods_per_year <= 0:
            raise ConfigError("periods_per_year must be positive and finite")
        if not math.isfinite(self.risk_free_rate):
            raise ConfigError("risk_free_rate must be finite")


@dataclass(frozen=True)
class Metrics:
    """Summary statistics; ``None`` marks a mathematically undefined value."""

    n_periods: int
    total_return: float
    annualised_return: float
    annualised_volatility: float | None
    sharpe: float | None
    sortino: float | None
    calmar: float | None
    max_drawdown: float
    max_drawdown_duration_periods: int
    max_drawdown_duration_ns: int
    n_trades: int
    hit_rate: float | None
    profit_factor: float | None
    turnover: float
    exposure: float
    avg_gross_exposure: float

    def to_dict(self) -> dict[str, float | int | None]:
        """JSON-safe mapping (no NaN/inf)."""
        return asdict(self)


def period_returns(equity: FloatArray) -> FloatArray:
    """Simple returns between consecutive equity points."""
    return np.asarray(np.diff(equity) / equity[:-1], dtype=np.float64)


def max_drawdown(equity: FloatArray, ts_ns: NDArray[np.int64]) -> tuple[float, int, int]:
    """Return ``(max drawdown fraction, duration in periods, duration in ns)``.

    Duration is that of the *longest* underwater stretch (peak to recovery).
    """
    peak = np.maximum.accumulate(equity)
    dd = float(np.max((peak - equity) / peak))
    underwater = equity < peak
    if not underwater.any():
        return dd, 0, 0
    padded = np.concatenate(([False], underwater, [False]))
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    starts, ends = edges[0::2], edges[1::2]  # run = [start, end) of underwater indices
    best_periods, best_ns = 0, 0
    for s, e in zip(starts, ends, strict=True):
        peak_idx = int(s) - 1
        recover_idx = min(int(e), len(equity) - 1)
        periods = recover_idx - peak_idx
        if periods > best_periods:
            best_periods = periods
            best_ns = int(ts_ns[recover_idx]) - int(ts_ns[peak_idx])
    return dd, best_periods, best_ns


def _ratio_stats(
    returns: FloatArray, cfg: MetricsConfig
) -> tuple[float | None, float | None, float | None]:
    """Return ``(annualised vol, sharpe, sortino)``."""
    if returns.shape[0] < 2:
        return None, None, None
    ann = math.sqrt(cfg.periods_per_year)
    excess = returns - cfg.risk_free_rate / cfg.periods_per_year
    std = float(np.std(returns, ddof=1))
    vol = std * ann
    sharpe = None if std == 0.0 else float(np.mean(excess)) / std * ann
    downside = math.sqrt(float(np.mean(np.minimum(excess, 0.0) ** 2)))
    sortino = None if downside == 0.0 else float(np.mean(excess)) / downside * ann
    return vol, sharpe, sortino


def _trade_stats(trades: Sequence[ClosedTrade]) -> tuple[float | None, float | None]:
    if not trades:
        return None, None
    nets = [t.net_pnl for t in trades]
    wins = sum((n for n in nets if n > 0), start=Decimal(0))
    losses = -sum((n for n in nets if n < 0), start=Decimal(0))
    hit = sum(1 for n in nets if n > 0) / len(nets)
    pf = None if losses == 0 else float(wins / losses)
    return hit, pf


def compute_metrics(
    ts_ns: NDArray[np.int64],
    equity: FloatArray,
    gross_exposure: FloatArray,
    trades: Sequence[ClosedTrade],
    traded_notional: Decimal,
    cfg: MetricsConfig,
) -> Metrics:
    """Compute :class:`Metrics` for one backtest (or one WFA window)."""
    if equity.shape[0] < 2:
        raise ConfigError("need at least two equity points to compute metrics")
    if not np.isfinite(equity).all() or (equity <= 0).any():
        raise ConfigError("equity must be finite and strictly positive")
    returns = period_returns(equity)
    n = int(returns.shape[0])
    total = float(equity[-1] / equity[0] - 1.0)
    ann_ret = (1.0 + total) ** (cfg.periods_per_year / n) - 1.0
    vol, sharpe, sortino = _ratio_stats(returns, cfg)
    dd, dd_periods, dd_ns = max_drawdown(equity, ts_ns)
    hit, pf = _trade_stats(trades)
    mean_eq = float(np.mean(equity))
    return Metrics(
        n_periods=n,
        total_return=total,
        annualised_return=ann_ret,
        annualised_volatility=vol,
        sharpe=sharpe,
        sortino=sortino,
        calmar=None if dd == 0.0 else ann_ret / dd,
        max_drawdown=dd,
        max_drawdown_duration_periods=dd_periods,
        max_drawdown_duration_ns=dd_ns,
        n_trades=len(trades),
        hit_rate=hit,
        profit_factor=pf,
        turnover=float(traded_notional) / mean_eq,
        exposure=float(np.mean(gross_exposure > 0.0)),
        avg_gross_exposure=float(np.mean(gross_exposure / equity)),
    )
