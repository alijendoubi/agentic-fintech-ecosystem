"""Synthetic market data for tests only (never used at runtime)."""

from __future__ import annotations

import numpy as np

REGIME_SPECS: tuple[tuple[float, float], ...] = (
    # (per-bar drift, per-bar return std)
    (0.0004, 0.0004),  # calm uptrend
    (-0.0004, 0.0004),  # calm downtrend
    (0.0, 0.0012),  # choppy, high vol
    (0.0, 0.0002),  # very quiet
    (-0.0010, 0.0035),  # crash-like
)


def make_columns(
    rng: np.random.Generator,
    n_per_regime: int = 90,
    order: tuple[int, ...] = (0, 1, 2, 3, 4),
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Concatenate regime segments into (mid_prices, spreads, ofis) columns."""
    log_price = float(np.log(100.0))
    prices: list[float | None] = []
    for regime in order:
        drift, std = REGIME_SPECS[regime]
        for _ in range(n_per_regime):
            log_price += float(rng.normal(drift, std))
            prices.append(float(np.exp(log_price)))
    n = len(prices)
    spreads: list[float | None] = [0.01 + float(abs(rng.normal(0.0, 0.004))) for _ in range(n)]
    ofis: list[float | None] = [float(rng.normal(0.0, 0.3)) for _ in range(n)]
    return prices, spreads, ofis
