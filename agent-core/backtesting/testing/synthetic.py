"""SYNTHETIC data generator - for tests and demos ONLY.

Nothing produced here is market data. Outputs must never be used as calibration or
backtest evidence, and must never be written into ``docs/``. Files written by
:func:`write_synthetic_csv` carry a ``# synthetic-data`` marker line that
``calibrate_ptc.py`` refuses to consume.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..data import SYNTHETIC_MARKER, MarketData, SymbolSeries
from ..models import RegimeLabel

DAY_NS = 86_400 * 1_000_000_000
DEFAULT_START_NS = 1_700_000_000 * 1_000_000_000  # arbitrary fixed instant, not a real bar


@dataclass(frozen=True)
class RegimeParams:
    """Per-regime drift and volatility of the SYNTHETIC log-return process (per bar)."""

    drift: float
    vol: float


DEFAULT_REGIME_PARAMS: Mapping[RegimeLabel, RegimeParams] = {
    RegimeLabel.REGIME_UNKNOWN: RegimeParams(0.0, 0.01),
    RegimeLabel.TRENDING_BULL: RegimeParams(0.002, 0.008),
    RegimeLabel.TRENDING_BEAR: RegimeParams(-0.002, 0.010),
    RegimeLabel.HIGH_VOL_CHOP: RegimeParams(0.0, 0.025),
    RegimeLabel.LOW_VOL_CHOP: RegimeParams(0.0, 0.004),
    RegimeLabel.CRISIS: RegimeParams(-0.006, 0.045),
}


def regime_schedule(n_bars: int, labels: Sequence[RegimeLabel], block: int) -> list[RegimeLabel]:
    """Cycle through ``labels`` in blocks of ``block`` bars, deterministically."""
    if block <= 0 or not labels:
        raise ValueError("block must be positive and labels non-empty")
    return [labels[(i // block) % len(labels)] for i in range(n_bars)]


def generate_market_data(
    symbols: Sequence[str],
    n_bars: int,
    *,
    seed: int,
    regimes: Sequence[RegimeLabel] | None = None,
    params: Mapping[RegimeLabel, RegimeParams] | None = None,
    start_ns: int = DEFAULT_START_NS,
    bar_ns: int = DAY_NS,
    base_price: float = 100.0,
    mean_volume: float = 1_000_000.0,
) -> MarketData:
    """Generate SYNTHETIC OHLCV bars from a seeded regime-switching random walk."""
    if n_bars < 2:
        raise ValueError("n_bars must be >= 2")
    table = dict(DEFAULT_REGIME_PARAMS if params is None else params)
    schedule = list(regimes) if regimes is not None else [RegimeLabel.REGIME_UNKNOWN] * n_bars
    if len(schedule) != n_bars:
        raise ValueError("regimes must have one label per bar")
    rng = np.random.default_rng(seed)
    ts = start_ns + bar_ns * np.arange(1, n_bars + 1, dtype=np.int64)
    drift = np.array([table[r].drift for r in schedule])
    vol = np.array([table[r].vol for r in schedule])
    reg = np.array([int(r) for r in schedule], dtype=np.int8)
    out: list[SymbolSeries] = []
    for symbol in symbols:
        rets = drift + vol * rng.standard_normal(n_bars)
        close = base_price * np.exp(np.cumsum(rets))
        open_ = np.concatenate(([base_price], close[:-1]))
        wiggle = np.abs(rng.standard_normal(n_bars)) * vol * 0.5
        high = np.maximum(open_, close) * (1.0 + wiggle)
        low = np.minimum(open_, close) * (1.0 - np.minimum(wiggle, 0.5))
        volume = mean_volume * (0.5 + rng.random(n_bars))
        out.append(SymbolSeries.create(symbol, ts, open_, high, low, close, volume, reg))
    return MarketData(out)


def write_synthetic_csv(path: Path, data: MarketData) -> None:
    """Write ``data`` as CSV with the ``# synthetic-data`` marker as the first line."""
    lines = [SYNTHETIC_MARKER, "timestamp_ns,symbol,open,high,low,close,volume,regime"]
    for symbol in data.symbols:
        s = data.series(symbol)
        for i in range(len(s)):
            b = s.bar(i)
            lines.append(
                f"{b.timestamp_ns},{b.symbol},{b.open!r},{b.high!r},{b.low!r},{b.close!r},"
                f"{b.volume!r},{b.regime.name}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
