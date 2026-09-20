"""Regime-aware walk-forward analysis (rolling or anchored) with purge and embargo.

Window geometry, in indices of the merged event timeline (all ends exclusive)::

    train nominal   [a, b)
    train effective [a, b - purge)         # purge: drop the tail whose labels/features
                                           # could overlap the test period
    test            [b + embargo, b + embargo + test_size)

so the gap between the last training sample and the first test sample is
``purge + embargo`` bars. ``rolling`` moves ``a`` by ``step``; ``anchored`` keeps ``a = 0``
and grows ``b``. ``step`` defaults to ``test_size`` and may not be smaller (out-of-sample
windows must not overlap, otherwise pooled OOS statistics double count).

Each window fits by exhaustive search over ``param_grid`` on the *training* slice only,
then evaluates the chosen parameters on the test slice with a fresh strategy instance.
``warmup_bars`` of earlier history (past data only) are visible to the strategy during the
test run so indicators can be primed, but no orders are placed before the test start.

Every configuration tried is counted (``n_configurations_tried``) so a multiple-testing
adjustment can be applied downstream; this module does not itself deflate Sharpe.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import StrEnum

import numpy as np
from numpy.typing import NDArray

from .data import MarketData
from .engine import BacktestConfig, BacktestResult, Strategy, run_backtest
from .metrics import FloatArray, Metrics, MetricsConfig, period_returns
from .models import HMM_STATES, BacktestError, ClosedTrade, ConfigError
from .regime_analysis import RegimeStats, regime_breakdown, summarise_returns

StrategyFactory = Callable[[Mapping[str, float]], Strategy]
OBJECTIVES = ("sharpe", "sortino", "calmar", "total_return")


class LeakageError(BacktestError):
    """Train/test windows violate the purge/embargo or overlap rules."""


class WindowMode(StrEnum):
    """Window family."""

    ROLLING = "rolling"
    ANCHORED = "anchored"


@dataclass(frozen=True)
class WindowSpec:
    """One split. ``train_end`` is the *effective* (already purged) exclusive end."""

    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


@dataclass(frozen=True)
class WfaConfig:
    """Walk-forward parameters."""

    train_size: int
    test_size: int
    step: int | None = None
    mode: WindowMode = WindowMode.ROLLING
    embargo: int = 0
    purge: int = 0
    warmup_bars: int = 0
    objective: str = "sharpe"
    min_regime_periods: int = 30  # below this a regime is "insufficient evidence"

    def __post_init__(self) -> None:
        if self.objective not in OBJECTIVES:
            raise ConfigError(f"objective must be one of {OBJECTIVES}, got {self.objective!r}")
        if self.warmup_bars < 0 or self.min_regime_periods < 1:
            raise ConfigError("warmup_bars must be >= 0 and min_regime_periods >= 1")


def generate_splits(
    n_samples: int,
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
    mode: WindowMode = WindowMode.ROLLING,
    embargo: int = 0,
    purge: int = 0,
) -> tuple[WindowSpec, ...]:
    """Build the window specs for ``n_samples`` timeline points (see module docstring)."""
    step = test_size if step is None else step
    if min(embargo, purge) < 0:
        raise ConfigError("embargo and purge must be non-negative")
    if train_size - purge < 3 or test_size < 2:
        raise ConfigError("need train_size - purge >= 3 and test_size >= 2")
    if step < test_size:
        raise ConfigError("step must be >= test_size so out-of-sample windows do not overlap")
    specs: list[WindowSpec] = []
    k = 0
    while True:
        a = k * step if mode is WindowMode.ROLLING else 0
        b = a + train_size if mode is WindowMode.ROLLING else train_size + k * step
        test_start = b + embargo
        if test_start + test_size > n_samples:
            break
        specs.append(WindowSpec(k, a, b - purge, test_start, test_start + test_size))
        k += 1
    if not specs:
        raise ConfigError(f"{n_samples} samples are too few for a single window")
    return tuple(specs)


def assert_no_leakage(splits: Sequence[WindowSpec], *, purge: int, embargo: int) -> None:
    """Raise :class:`LeakageError` unless every split honours the purge/embargo gap."""
    for s in splits:
        if s.train_end > s.test_start or s.train_start >= s.train_end:
            raise LeakageError(f"window {s.index}: training range overlaps or follows test range")
        if s.test_start - s.train_end < purge + embargo:
            raise LeakageError(
                f"window {s.index}: gap {s.test_start - s.train_end} < purge+embargo "
                f"{purge + embargo}"
            )
    for prev, nxt in itertools.pairwise(splits):
        if nxt.test_start < prev.test_end:
            raise LeakageError(f"windows {prev.index}/{nxt.index}: test ranges overlap")


@dataclass(frozen=True)
class WindowResult:
    """Outcome of one window."""

    spec: WindowSpec
    best_params: dict[str, float]
    train_metrics: Metrics
    test_metrics: Metrics
    test_returns: FloatArray
    test_regimes: NDArray[np.int8]
    test_trades: tuple[ClosedTrade, ...]
    regimes: dict[str, RegimeStats]

    def to_dict(self) -> dict[str, object]:
        """JSON-safe summary (arrays omitted)."""
        return {
            "spec": asdict(self.spec),
            "best_params": self.best_params,
            "train_metrics": self.train_metrics.to_dict(),
            "test_metrics": self.test_metrics.to_dict(),
            "regimes": {k: v.to_dict() for k, v in self.regimes.items()},
        }


@dataclass(frozen=True)
class AggregateStats:
    """Cross-window aggregates and stability diagnostics."""

    n_windows: int
    oos_sharpe_mean: float | None
    oos_sharpe_median: float | None
    oos_sharpe_std: float | None
    oos_sharpe_cv: float | None
    oos_total_return_mean: float
    positive_window_fraction: float
    walk_forward_efficiency: float | None
    param_stability: float
    n_configurations_tried: int
    pooled_oos: RegimeStats
    regimes: dict[str, RegimeStats]
    missing_regimes: tuple[str, ...]
    min_regime_periods: int

    def to_dict(self) -> dict[str, object]:
        """JSON-safe mapping."""
        out = asdict(self)
        out["pooled_oos"] = self.pooled_oos.to_dict()
        out["regimes"] = {k: v.to_dict() for k, v in self.regimes.items()}
        out["missing_regimes"] = list(self.missing_regimes)
        return out


@dataclass(frozen=True)
class WfaResult:
    """Full walk-forward output."""

    config: WfaConfig
    windows: tuple[WindowResult, ...]
    aggregate: AggregateStats

    def to_dict(self) -> dict[str, object]:
        """JSON-safe mapping (no NaN/inf, no arrays)."""
        return {
            "config": asdict(self.config),
            "windows": [w.to_dict() for w in self.windows],
            "aggregate": self.aggregate.to_dict(),
        }


def walk_forward_efficiency(
    in_sample: Sequence[float], out_of_sample: Sequence[float]
) -> float | None:
    """Mean OOS score divided by mean IS score; ``None`` unless the IS mean is positive."""
    if not in_sample or not out_of_sample:
        return None
    mean_is = float(np.mean(in_sample))
    if mean_is <= 0.0:
        return None
    return float(np.mean(out_of_sample)) / mean_is


def _score(m: Metrics, objective: str) -> float:
    value = m.total_return if objective == "total_return" else getattr(m, objective)
    return -math.inf if value is None else float(value)


def _expand_grid(grid: Mapping[str, Sequence[float]]) -> list[dict[str, float]]:
    keys = sorted(grid)
    if any(len(grid[k]) == 0 for k in keys):
        raise ConfigError("every param_grid entry needs at least one value")
    combos = itertools.product(*(grid[k] for k in keys))
    return [dict(zip(keys, combo, strict=True)) for combo in combos]


def _fit_window(
    data: MarketData,
    factory: StrategyFactory,
    combos: Sequence[dict[str, float]],
    spec: WindowSpec,
    cfg: WfaConfig,
    bt: BacktestConfig,
) -> tuple[dict[str, float], BacktestResult]:
    tl = data.timeline
    train = data.slice_time(int(tl[spec.train_start]), int(tl[spec.train_end - 1]))
    best: tuple[float, dict[str, float], BacktestResult] | None = None
    for params in combos:
        res = run_backtest(train, factory(params), bt)
        score = _score(res.metrics, cfg.objective)
        if best is None or score > best[0]:
            best = (score, params, res)
    assert best is not None  # combos is never empty
    return best[1], best[2]


def _evaluate_window(
    data: MarketData,
    factory: StrategyFactory,
    params: Mapping[str, float],
    spec: WindowSpec,
    cfg: WfaConfig,
    bt: BacktestConfig,
) -> BacktestResult:
    tl = data.timeline
    first = max(0, spec.test_start - cfg.warmup_bars)
    test = data.slice_time(int(tl[first]), int(tl[spec.test_end - 1]))
    test_bt = replace(bt, trading_start_ts_ns=int(tl[spec.test_start]))
    return run_backtest(test, factory(params), test_bt)


def _aggregate(
    windows: Sequence[WindowResult],
    train_scores: Sequence[float],
    n_combos: int,
    cfg: WfaConfig,
    metrics_cfg: MetricsConfig,
) -> AggregateStats:
    returns = np.concatenate([w.test_returns for w in windows])
    regimes = np.concatenate([w.test_regimes for w in windows])
    trades = [t for w in windows for t in w.test_trades]
    pooled_regimes = regime_breakdown(
        returns, regimes, trades, metrics_cfg, min_periods=cfg.min_regime_periods
    )
    sharpes = [w.test_metrics.sharpe for w in windows if w.test_metrics.sharpe is not None]
    mean_sharpe = float(np.mean(sharpes)) if sharpes else None
    std_sharpe = float(np.std(sharpes, ddof=1)) if len(sharpes) > 1 else None
    cv = None if not std_sharpe or not mean_sharpe else std_sharpe / abs(mean_sharpe)
    scored = (_score(w.test_metrics, cfg.objective) for w in windows)
    oos_scores = [x for x in scored if math.isfinite(x)]
    modal = Counter(tuple(sorted(w.best_params.items())) for w in windows).most_common(1)[0][1]
    totals = [w.test_metrics.total_return for w in windows]
    return AggregateStats(
        n_windows=len(windows),
        oos_sharpe_mean=mean_sharpe,
        oos_sharpe_median=float(np.median(sharpes)) if sharpes else None,
        oos_sharpe_std=std_sharpe,
        oos_sharpe_cv=cv,
        oos_total_return_mean=float(np.mean(totals)),
        positive_window_fraction=float(np.mean([t > 0 for t in totals])),
        walk_forward_efficiency=walk_forward_efficiency(train_scores, oos_scores),
        param_stability=modal / len(windows),
        n_configurations_tried=n_combos * len(windows),
        pooled_oos=summarise_returns(
            "ALL", returns, trades, metrics_cfg, min_periods=cfg.min_regime_periods
        ),
        regimes=pooled_regimes,
        missing_regimes=tuple(s.name for s in HMM_STATES if pooled_regimes[s.name].n_periods == 0),
        min_regime_periods=cfg.min_regime_periods,
    )


def run_walk_forward(
    data: MarketData,
    factory: StrategyFactory,
    param_grid: Mapping[str, Sequence[float]],
    cfg: WfaConfig,
    backtest_config: BacktestConfig,
) -> WfaResult:
    """Run the full walk-forward procedure and aggregate per window and per regime."""
    combos = _expand_grid(param_grid)
    specs = generate_splits(
        int(data.timeline.shape[0]),
        train_size=cfg.train_size, test_size=cfg.test_size, step=cfg.step, mode=cfg.mode,
        embargo=cfg.embargo, purge=cfg.purge,
    )  # fmt: skip
    assert_no_leakage(specs, purge=cfg.purge, embargo=cfg.embargo)
    results: list[WindowResult] = []
    train_scores: list[float] = []
    for spec in specs:
        params, train_res = _fit_window(data, factory, combos, spec, cfg, backtest_config)
        test_res = _evaluate_window(data, factory, params, spec, cfg, backtest_config)
        rets = period_returns(test_res.equity.equity)
        regs = np.asarray(test_res.equity.regime[:-1], dtype=np.int8)
        train_scores.append(_score(train_res.metrics, cfg.objective))
        results.append(
            WindowResult(
                spec, dict(params), train_res.metrics, test_res.metrics, rets, regs,
                test_res.trades,
                regime_breakdown(
                    rets, regs, test_res.trades, backtest_config.metrics,
                    min_periods=cfg.min_regime_periods,
                ),
            )
        )  # fmt: skip
    finite = [s for s in train_scores if math.isfinite(s)]
    agg = _aggregate(results, finite, len(combos), cfg, backtest_config.metrics)
    return WfaResult(cfg, tuple(results), agg)
