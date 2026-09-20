"""Empirical statistics for pre-trade-control calibration (pure functions, no I/O).

Nothing here contains a default threshold or example number: every value returned is
computed from the supplied data.

Conventions
-----------
* Bars are close-stamped (see ``data.py``). Regular trading hours (RTH) are
  ``09:30 < stamp <= 16:00`` America/New_York.
* A "1-bar return" is ``close_t / close_{t-1} - 1`` between two consecutive RTH bars of the
  same symbol exactly ``bar_seconds`` apart. Overnight, weekend and missing-bar gaps are
  never bridged.
* Percentile confidence intervals are distribution-free (order-statistic ranks from the
  binomial distribution). ``reliable`` is False when fewer than ``MIN_TAIL_OBSERVATIONS``
  observations lie beyond the percentile, i.e. the estimate is decided by a handful of
  points; ``ci_truncated`` flags an upper rank beyond the sample maximum.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from numpy.typing import NDArray
from scipy import stats

ET_ZONE = "America/New_York"
RTH_OPEN_S = 9 * 3600 + 30 * 60
RTH_CLOSE_S = 16 * 3600
MIN_TAIL_OBSERVATIONS = 10
PERCENTILES = (0.95, 0.99, 0.999)
NS_PER_S = 1_000_000_000

FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class PercentileEstimate:
    """A sample percentile with a distribution-free confidence interval."""

    q: float
    value: float
    ci_low: float
    ci_high: float
    n_obs: int
    n_tail: float
    reliable: bool
    ci_truncated: bool

    def to_dict(self) -> dict[str, float | int | bool]:
        """Report-friendly mapping (values also given in percent)."""
        out = asdict(self)
        out["value_pct"] = self.value * 100.0
        out["ci_low_pct"] = self.ci_low * 100.0
        out["ci_high_pct"] = self.ci_high * 100.0
        return out


def percentile_key(q: float) -> str:
    """``0.999`` -> ``"p99.9"``."""
    return f"p{q * 100:g}"


def percentile_with_ci(
    values: FloatArray, q: float, confidence: float = 0.95
) -> PercentileEstimate:
    """Sample percentile ``q`` (0-1) of ``values`` with a binomial order-statistic CI."""
    n = int(values.shape[0])
    if n == 0:
        raise ValueError("cannot estimate a percentile of an empty sample")
    if not 0.0 < q < 1.0:
        raise ValueError("q must be within (0, 1)")
    ordered = np.sort(values)
    alpha = 1.0 - confidence
    lo_rank = int(stats.binom.ppf(alpha / 2.0, n, q))
    hi_rank = int(stats.binom.ppf(1.0 - alpha / 2.0, n, q)) + 1
    lo_idx = min(max(lo_rank - 1, 0), n - 1)
    hi_idx = min(max(hi_rank - 1, 0), n - 1)
    n_tail = n * (1.0 - q)
    return PercentileEstimate(
        q=q,
        value=float(np.quantile(ordered, q)),
        ci_low=float(ordered[lo_idx]),
        ci_high=float(ordered[hi_idx]),
        n_obs=n,
        n_tail=n_tail,
        reliable=n_tail >= MIN_TAIL_OBSERVATIONS,
        ci_truncated=hi_rank > n,
    )


def percentile_table(
    values: FloatArray, qs: tuple[float, ...] = PERCENTILES
) -> dict[str, dict[str, float | int | bool]]:
    """Percentile estimates keyed ``p95``/``p99``/``p99.9``."""
    return {percentile_key(q): percentile_with_ci(values, q).to_dict() for q in qs}


def min_observations(qs: tuple[float, ...] = PERCENTILES) -> int:
    """Smallest sample whose highest percentile has ``MIN_TAIL_OBSERVATIONS`` tail points."""
    return math.ceil(MIN_TAIL_OBSERVATIONS / (1.0 - max(qs)))


def _local_arrays(ts_ns: NDArray[np.int64]) -> tuple[NDArray[np.int64], NDArray[np.datetime64]]:
    """Seconds-of-day and local calendar date (America/New_York) for each UTC stamp."""
    local = pd.DatetimeIndex(pd.to_datetime(ts_ns, unit="ns", utc=True)).tz_convert(ET_ZONE)
    sod = (local.hour * 3600 + local.minute * 60 + local.second).to_numpy(dtype=np.int64)
    dates = local.tz_localize(None).to_numpy().astype("datetime64[D]")
    return sod, dates


def sorted_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Frame sorted by (symbol, ts_ns) with a clean index."""
    return frame.sort_values(["symbol", "ts_ns"], kind="stable").reset_index(drop=True)


def rth_mask(frame: pd.DataFrame) -> NDArray[np.bool_]:
    """True for rows inside regular trading hours (``frame`` must be time-sortable)."""
    sod, _ = _local_arrays(frame["ts_ns"].to_numpy(dtype=np.int64))
    return np.asarray((sod > RTH_OPEN_S) & (sod <= RTH_CLOSE_S), dtype=np.bool_)


def abs_bar_returns(frame: pd.DataFrame, bar_seconds: int) -> dict[str, FloatArray]:
    """Absolute 1-bar RTH returns per symbol (see module conventions)."""
    df = sorted_frame(frame)
    sym = df["symbol"].to_numpy()
    ts = df["ts_ns"].to_numpy(dtype=np.int64)
    close = df["close"].to_numpy(dtype=np.float64)
    inside = rth_mask(df)
    ok = (
        (sym[1:] == sym[:-1])
        & (ts[1:] - ts[:-1] == bar_seconds * NS_PER_S)
        & inside[1:]
        & inside[:-1]
    )
    rets = np.abs(close[1:] / close[:-1] - 1.0)
    out: dict[str, FloatArray] = {}
    for name in np.unique(sym):
        sel = ok & (sym[1:] == name)
        out[str(name)] = rets[sel]
    return out


def daily_stats(frame: pd.DataFrame) -> pd.DataFrame:
    """Per symbol-day RTH stats: close-to-close daily return and intraday drawdown.

    ``intraday_drawdown`` = (first RTH close - lowest RTH close) / first RTH close (>= 0),
    the worst adverse excursion of a long position entered at the first RTH bar.
    ``daily_return`` uses the previous *available* day of the same symbol.
    """
    df = sorted_frame(frame)
    df = df.loc[rth_mask(df)].copy()
    _, dates = _local_arrays(df["ts_ns"].to_numpy(dtype=np.int64))
    df["date"] = dates
    grouped = df.groupby(["symbol", "date"], sort=True)["close"].agg(["first", "last", "min"])
    grouped = grouped.reset_index()
    prev_last = grouped.groupby("symbol")["last"].shift(1)
    grouped["daily_return"] = grouped["last"] / prev_last - 1.0
    grouped["intraday_drawdown"] = (grouped["first"] - grouped["min"]) / grouped["first"]
    return grouped
