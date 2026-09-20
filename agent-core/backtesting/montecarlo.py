"""Seeded, vectorised Monte Carlo bootstrap of trade (or period) returns.

Method
------
Each path resamples ``horizon`` returns (default: as many as the input) either

* ``IID``: independent draws with replacement, or
* ``BLOCK``: circular moving-block bootstrap with blocks of ``block_size`` consecutive
  returns (start indices uniform, wrapping around), preserving short-range dependence.

Returns are optionally multiplied by ``scale`` (a fixed fraction of equity risked) and then
*compounded*: ``equity_t = prod(1 + r_i)`` from a start of 1.0. Per path we record

* ``terminal_return = equity_T - 1``;
* ``max_drawdown = max_t (peak_t - equity_t) / peak_t`` (peak includes the starting 1.0).

Reproducibility: each chunk of paths draws from its own ``SeedSequence(seed, spawn_key=(i,))``
stream, so results depend only on ``(returns, config)``, never on machine or thread count.

Ruin is **not defined by the repository**: ``p_ruin`` is computed only when the caller passes
an explicit ``ruin_drawdown`` (drawdown from peak at which the account is deemed ruined) and
is ``None`` otherwise. The output is an estimate under the resampling assumptions, not a
guarantee.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray

from .models import ClosedTrade, ConfigError

DEFAULT_PATHS = 50_000
_MAX_CHUNK_PATHS = 5_000
_CELL_BUDGET = 4_000_000  # max (paths x steps) float cells per chunk, bounds memory
_QUANTILES_PCT = (0.1, 1.0, 5.0, 25.0, 50.0, 75.0, 95.0, 99.0, 99.9)
_CONFIDENCES = (0.95, 0.99, 0.999)


class BootstrapMethod(StrEnum):
    """Resampling scheme."""

    IID = "iid"
    BLOCK = "block"


@dataclass(frozen=True)
class MonteCarloConfig:
    """Monte Carlo parameters."""

    seed: int
    n_paths: int = DEFAULT_PATHS
    method: BootstrapMethod = BootstrapMethod.IID
    block_size: int = 1
    horizon: int | None = None
    scale: float = 1.0
    ruin_drawdown: float | None = None

    def __post_init__(self) -> None:
        if self.n_paths < 1:
            raise ConfigError("n_paths must be >= 1")
        if self.block_size < 1:
            raise ConfigError("block_size must be >= 1")
        if self.horizon is not None and self.horizon < 1:
            raise ConfigError("horizon must be >= 1")
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise ConfigError("scale must be positive and finite")
        if self.ruin_drawdown is not None and not 0.0 < self.ruin_drawdown <= 1.0:
            raise ConfigError("ruin_drawdown must be in (0, 1]")


def _pkey(q: float) -> str:
    return f"p{q:g}"


def _distribution(x: NDArray[np.float64], pcts: tuple[float, ...]) -> dict[str, float]:
    out = {"mean": float(x.mean()), "std": float(x.std()), "median": float(np.median(x))}
    for q, v in zip(pcts, np.percentile(x, pcts), strict=True):
        out[_pkey(q)] = float(v)
    return out


@dataclass(frozen=True)
class MonteCarloResult:
    """Per-path outcomes plus summary helpers."""

    config: MonteCarloConfig
    n_source_returns: int
    horizon: int
    max_drawdown: NDArray[np.float64]
    terminal_return: NDArray[np.float64]

    def summary(self) -> dict[str, Any]:
        """JSON-safe summary: distributions, tail percentiles, loss/ruin probabilities.

        ``max_drawdown`` percentiles are upper-tail (higher = worse). For
        ``terminal_return`` the adverse tail is reported as ``conf_95`` / ``conf_99`` /
        ``conf_99.9``: the value that terminal return falls *below* with probability 5%,
        1% and 0.1% respectively.
        """
        adverse = {
            f"conf_{c * 100:g}": float(np.percentile(self.terminal_return, (1.0 - c) * 100.0))
            for c in _CONFIDENCES
        }
        ruin = self.config.ruin_drawdown
        return {
            "n_paths": self.config.n_paths,
            "n_source_returns": self.n_source_returns,
            "horizon": self.horizon,
            "method": str(self.config.method),
            "block_size": self.config.block_size,
            "seed": self.config.seed,
            "scale": self.config.scale,
            "max_drawdown": _distribution(self.max_drawdown, _QUANTILES_PCT),
            "terminal_return": _distribution(self.terminal_return, _QUANTILES_PCT),
            "terminal_return_adverse": adverse,
            "p_loss": float(np.mean(self.terminal_return < 0.0)),
            "ruin_drawdown": ruin,
            "p_ruin": None if ruin is None else float(np.mean(self.max_drawdown >= ruin)),
        }


def _validate(returns: ArrayLike, cfg: MonteCarloConfig) -> NDArray[np.float64]:
    r = np.asarray(returns, dtype=np.float64)
    if r.ndim != 1 or r.shape[0] < 2:
        raise ConfigError("returns must be a 1-D array with at least 2 observations")
    if not np.isfinite(r).all():
        raise ConfigError("returns must be finite")
    scaled = r * cfg.scale
    if (scaled <= -1.0).any():
        raise ConfigError("a return <= -100% cannot be compounded; check units (fractions)")
    if cfg.method is BootstrapMethod.BLOCK and cfg.block_size > r.shape[0]:
        raise ConfigError("block_size cannot exceed the number of source returns")
    return scaled


def _draw(
    rng: np.random.Generator, src: NDArray[np.float64], m: int, length: int, cfg: MonteCarloConfig
) -> NDArray[np.float64]:
    n = src.shape[0]
    if cfg.method is BootstrapMethod.IID:
        return np.asarray(src[rng.integers(0, n, size=(m, length))], dtype=np.float64)
    b = cfg.block_size
    n_blocks = -(-length // b)
    starts = rng.integers(0, n, size=(m, n_blocks))
    idx = (starts[:, :, None] + np.arange(b)) % n
    return np.asarray(src[idx.reshape(m, n_blocks * b)[:, :length]], dtype=np.float64)


def _chunk_outcomes(
    draws: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    equity = np.cumprod(1.0 + draws, axis=1)
    peak = np.maximum(np.maximum.accumulate(equity, axis=1), 1.0)
    drawdown = ((peak - equity) / peak).max(axis=1)
    return drawdown, equity[:, -1] - 1.0


def run_monte_carlo(returns: ArrayLike, cfg: MonteCarloConfig) -> MonteCarloResult:
    """Bootstrap ``returns`` (fractional, e.g. 0.01 = +1%) into ``cfg.n_paths`` equity paths."""
    src = _validate(returns, cfg)
    length = cfg.horizon if cfg.horizon is not None else int(src.shape[0])
    chunk = max(1, min(_MAX_CHUNK_PATHS, _CELL_BUDGET // length))
    mdd = np.empty(cfg.n_paths, dtype=np.float64)
    term = np.empty(cfg.n_paths, dtype=np.float64)
    for i, lo in enumerate(range(0, cfg.n_paths, chunk)):
        hi = min(lo + chunk, cfg.n_paths)
        rng = np.random.default_rng(np.random.SeedSequence(cfg.seed, spawn_key=(i,)))
        mdd[lo:hi], term[lo:hi] = _chunk_outcomes(_draw(rng, src, hi - lo, length, cfg))
    mdd.setflags(write=False)
    term.setflags(write=False)
    return MonteCarloResult(cfg, int(src.shape[0]), length, mdd, term)


def trade_returns(trades: Sequence[ClosedTrade]) -> NDArray[np.float64]:
    """Net return of each round trip on its entry notional (input for :func:`run_monte_carlo`).

    These are returns on *position* notional; pass ``scale`` (fraction of equity deployed per
    trade) to :class:`MonteCarloConfig` to convert them into equity returns.
    """
    return np.asarray([t.return_fraction for t in trades], dtype=np.float64)
