"""Feature extraction and standardisation for the regime HMM.

Feature columns (per bar):
  0  log_return    1-bar log return of the mid price
  1  realized_vol  rolling annualised std of log returns
  2  spread_norm   spread / mid price
  3  ofi           order flow imbalance

Bad data is never repaired: any row with a non-finite value in any column is
dropped *jointly* (all columns together), so the matrix stays row-aligned and
no fake zeros are introduced (ALI-29).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

N_FEATURES = 4
RVOL_WINDOW = 20
RVOL_MIN_PERIODS = 5
#: Annualisation factor for 1-second bars over a 6.5h US session, 252 days.
ANNUALISATION = float(np.sqrt(252 * 6.5 * 3600))
#: Standard deviations at or below this are treated as constant features.
SCALE_FLOOR = 1e-12


@dataclass(frozen=True, slots=True)
class FeatureBuild:
    """Result of feature extraction. ``matrix`` is None when nothing usable exists."""

    matrix: np.ndarray | None
    rows_in: int
    rows_used: int
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Scaler:
    """Per-feature standardiser; parameters are persisted with the model."""

    mean: np.ndarray
    scale: np.ndarray

    @classmethod
    def fit(cls, matrix: np.ndarray) -> Scaler:
        """Fit on a finite (N, 4) matrix; constant columns get scale 1 (become 0)."""
        mean = matrix.mean(axis=0)
        std = matrix.std(axis=0, ddof=0)
        scale = np.where(std > SCALE_FLOOR, std, 1.0)
        return cls(mean=_frozen(mean), scale=_frozen(scale))

    def transform(self, matrix: np.ndarray) -> np.ndarray:
        """Return a new standardised matrix; the input is untouched."""
        return (matrix - self.mean) / self.scale


def _frozen(array: np.ndarray) -> np.ndarray:
    copy = np.array(array, dtype=np.float64, copy=True)
    copy.setflags(write=False)
    return copy


def _to_float_array(values: Sequence[float | None]) -> np.ndarray:
    """Convert to float64; ``None`` and unparsable entries become NaN (dropped later)."""
    out = np.full(len(values), np.nan, dtype=np.float64)
    for i, value in enumerate(values):
        if value is None or isinstance(value, bool):
            continue
        try:
            out[i] = float(value)
        except (TypeError, ValueError):
            continue
    out[~np.isfinite(out)] = np.nan
    return out


def _rolling_vol(log_returns: np.ndarray) -> np.ndarray:
    """Annualised rolling std over finite returns; NaN until enough periods exist."""
    out = np.full(len(log_returns), np.nan, dtype=np.float64)
    for i in range(len(log_returns)):
        chunk = log_returns[max(0, i - RVOL_WINDOW + 1) : i + 1]
        finite = chunk[np.isfinite(chunk)]
        if len(finite) >= RVOL_MIN_PERIODS:
            out[i] = np.std(finite, ddof=1) * ANNUALISATION
    return out


def extract_features(
    mid_prices: Sequence[float | None],
    spreads: Sequence[float | None],
    ofis: Sequence[float | None],
) -> FeatureBuild:
    """Build the (N, 4) feature matrix from equal-length, time-ordered columns.

    Inputs must be row-aligned (same length). ``None``/NaN/inf/non-positive
    prices mark a bad cell; the whole row is dropped, never zero-filled.
    """
    n = len(mid_prices)
    if len(spreads) != n or len(ofis) != n:
        return FeatureBuild(None, n, 0, "column_length_mismatch")
    if n < 2:
        return FeatureBuild(None, n, 0, "too_few_rows")

    prices = _to_float_array(mid_prices)
    prices[prices <= 0.0] = np.nan
    spread_arr = _to_float_array(spreads)
    ofi_arr = _to_float_array(ofis)

    with np.errstate(invalid="ignore", divide="ignore"):
        log_returns = np.diff(np.log(prices))  # NaN wherever either price is bad
        rvol = _rolling_vol(log_returns)
        spread_norm = spread_arr[1:] / prices[1:]

    matrix = np.column_stack([log_returns, rvol, spread_norm, ofi_arr[1:]])
    keep = np.isfinite(matrix).all(axis=1)
    used = int(keep.sum())
    if used == 0:
        return FeatureBuild(None, n, 0, "no_finite_rows")
    return FeatureBuild(matrix[keep], n, used)
