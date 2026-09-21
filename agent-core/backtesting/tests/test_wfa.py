"""Walk-forward analysis tests: splits, leakage embargo, per-regime breakdown. SYNTHETIC data."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal as D

import numpy as np
import pytest

from backtesting.data import MarketData
from backtesting.engine import BacktestConfig, Strategy
from backtesting.feed import MarketView
from backtesting.metrics import MetricsConfig
from backtesting.models import (
    HMM_STATES,
    ClosedTrade,
    ConfigError,
    OrderIntent,
    RegimeLabel,
)
from backtesting.portfolio import PortfolioSnapshot
from backtesting.regime_analysis import regime_breakdown
from backtesting.strategies import buy_and_hold
from backtesting.testing.synthetic import (
    RegimeParams,
    generate_market_data,
    regime_schedule,
)
from backtesting.wfa import (
    LeakageError,
    WfaConfig,
    WindowMode,
    WindowSpec,
    assert_no_leakage,
    generate_splits,
    run_walk_forward,
    walk_forward_efficiency,
)

# ------------------------------------------------------------------- splits


def test_rolling_splits_exact_indices_with_purge_and_embargo() -> None:
    sp = generate_splits(100, train_size=30, test_size=10, step=10, purge=3, embargo=2)
    assert len(sp) == 6
    w0, w1 = sp[0], sp[1]
    assert (w0.train_start, w0.train_end, w0.test_start, w0.test_end) == (0, 27, 32, 42)
    assert (w1.train_start, w1.train_end, w1.test_start, w1.test_end) == (10, 37, 42, 52)
    assert sp[-1].test_end <= 100
    assert all(s.test_start - s.train_end >= 5 for s in sp)  # purge + embargo gap
    assert_no_leakage(sp, purge=3, embargo=2)


def test_anchored_splits_grow_from_zero() -> None:
    sp = generate_splits(60, train_size=20, test_size=10, mode=WindowMode.ANCHORED, embargo=1)
    assert all(s.train_start == 0 for s in sp)
    assert [s.train_end for s in sp] == [20, 30, 40]
    assert [s.test_start for s in sp] == [21, 31, 41]
    assert_no_leakage(sp, purge=0, embargo=1)


def test_test_windows_never_overlap_each_other() -> None:
    sp = generate_splits(200, train_size=50, test_size=20, embargo=5)
    for a, b in zip(sp, sp[1:], strict=False):
        assert a.test_end <= b.test_start


def test_assert_no_leakage_detects_violations() -> None:
    bad_gap = [WindowSpec(0, 0, 30, 31, 41)]
    with pytest.raises(LeakageError, match="gap"):
        assert_no_leakage(bad_gap, purge=0, embargo=5)
    overlap = [WindowSpec(0, 0, 45, 40, 50)]
    with pytest.raises(LeakageError):
        assert_no_leakage(overlap, purge=0, embargo=0)
    test_overlap = [WindowSpec(0, 0, 10, 12, 22), WindowSpec(1, 5, 15, 20, 30)]
    with pytest.raises(LeakageError, match="overlap"):
        assert_no_leakage(test_overlap, purge=0, embargo=0)


def test_split_parameter_validation() -> None:
    with pytest.raises(ConfigError):
        generate_splits(100, train_size=30, test_size=10, purge=30)
    with pytest.raises(ConfigError):
        generate_splits(100, train_size=30, test_size=10, step=5)  # would overlap OOS
    with pytest.raises(ConfigError):
        generate_splits(20, train_size=30, test_size=10)  # no window fits
    with pytest.raises(ConfigError):
        generate_splits(100, train_size=30, test_size=10, embargo=-1)


# ----------------------------------------------------- behavioural leakage


def test_engine_windows_never_see_data_past_their_own_range_and_respect_the_gap() -> None:
    data = generate_market_data(["AAA"], 120, seed=21)
    tl = data.timeline
    runs: list[list[int]] = []

    def factory(params: Mapping[str, float]) -> Strategy:
        seen: list[int] = []
        runs.append(seen)

        def strat(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
            seen.append(view.now_ns)
            return []

        return strat

    cfg = WfaConfig(train_size=40, test_size=10, purge=3, embargo=2, warmup_bars=5)
    res = run_walk_forward(data, factory, {"x": [1.0]}, cfg, BacktestConfig())
    assert len(runs) == 2 * len(res.windows)  # one train run + one test run per window
    for k, w in enumerate(res.windows):
        train_calls, test_calls = runs[2 * k], runs[2 * k + 1]
        assert max(train_calls) == int(tl[w.spec.train_end - 1])
        assert min(test_calls) == int(tl[w.spec.test_start])  # trading only from test start
        assert max(test_calls) == int(tl[w.spec.test_end - 1])
        gap_bars = w.spec.test_start - w.spec.train_end
        assert gap_bars >= cfg.purge + cfg.embargo
        assert max(train_calls) < min(test_calls)


# ------------------------------------------------------ regime breakdown


def test_regime_breakdown_hand_computed() -> None:
    returns = np.array([0.10, -0.05, 0.02, 0.02, -0.01])
    regimes = np.array([1, 1, 2, 2, 2], dtype=np.int8)
    trade = ClosedTrade(
        "A", "LONG", 0, 1, D(1), D(100), D(110), D(10), D(0), RegimeLabel.TRENDING_BULL
    )
    out = regime_breakdown(returns, regimes, [trade], MetricsConfig(1.0), min_periods=2)
    bull = out["TRENDING_BULL"]
    assert bull.n_periods == 2 and bull.n_trades == 1
    assert bull.total_return == pytest.approx(1.10 * 0.95 - 1)
    assert bull.mean_return == pytest.approx(0.025)
    assert bull.max_drawdown == pytest.approx(0.05)
    assert not bull.insufficient_evidence
    bear = out["TRENDING_BEAR"]
    assert bear.n_periods == 3 and bear.n_trades == 0 and bear.hit_rate is None
    # Regimes with no observations are still reported, flagged, never silently dropped.
    for name in ("CRISIS", "HIGH_VOL_CHOP", "LOW_VOL_CHOP"):
        assert out[name].n_periods == 0 and out[name].insufficient_evidence
        assert out[name].total_return is None


def test_insufficient_evidence_threshold_is_explicit() -> None:
    returns = np.array([0.01, 0.02, 0.03])
    regimes = np.array([1, 1, 1], dtype=np.int8)
    low = regime_breakdown(returns, regimes, [], MetricsConfig(1.0), min_periods=4)
    high = regime_breakdown(returns, regimes, [], MetricsConfig(1.0), min_periods=3)
    assert low["TRENDING_BULL"].insufficient_evidence
    assert not high["TRENDING_BULL"].insufficient_evidence


# -------------------------------------------------------- full walk-forward


def _regime_data(seed: int = 3) -> MarketData:
    labels = [RegimeLabel.TRENDING_BULL, RegimeLabel.TRENDING_BEAR, RegimeLabel.CRISIS]
    params = {
        RegimeLabel.TRENDING_BULL: RegimeParams(0.010, 0.001),
        RegimeLabel.TRENDING_BEAR: RegimeParams(-0.010, 0.001),
        RegimeLabel.CRISIS: RegimeParams(-0.020, 0.002),
    }
    sched = regime_schedule(600, labels, block=50)
    return generate_market_data(["AAA"], 600, seed=seed, regimes=sched, params=params)


def _long_or_flat(params: Mapping[str, float]) -> Strategy:
    if params["long"] == 1.0:
        return buy_and_hold("AAA")
    return lambda view, port: []


def test_per_regime_results_reflect_synthetic_drift_and_cover_all_states() -> None:
    data = _regime_data()
    cfg = WfaConfig(train_size=100, test_size=100, min_regime_periods=20)
    res = run_walk_forward(data, _long_or_flat, {"long": [1.0]}, cfg, BacktestConfig())
    pooled = res.aggregate.regimes
    assert set(pooled) >= {s.name for s in HMM_STATES}
    assert pooled["TRENDING_BULL"].mean_return is not None
    assert pooled["TRENDING_BULL"].mean_return > 0 > pooled["TRENDING_BEAR"].mean_return  # type: ignore[operator]
    assert pooled["CRISIS"].total_return is not None and pooled["CRISIS"].total_return < 0
    # Unobserved states are explicit, not omitted.
    assert pooled["HIGH_VOL_CHOP"].n_periods == 0 and pooled["HIGH_VOL_CHOP"].insufficient_evidence
    assert set(res.aggregate.missing_regimes) == {"HIGH_VOL_CHOP", "LOW_VOL_CHOP"}
    total = sum(s.n_periods for s in pooled.values())
    assert total == sum(w.test_returns.shape[0] for w in res.windows)


def test_selection_picks_better_param_and_reports_stability_and_trials() -> None:
    data = generate_market_data(
        ["AAA"], 400, seed=9, params={RegimeLabel.REGIME_UNKNOWN: RegimeParams(0.004, 0.002)}
    )
    cfg = WfaConfig(train_size=100, test_size=50, objective="total_return")
    res = run_walk_forward(data, _long_or_flat, {"long": [0.0, 1.0]}, cfg, BacktestConfig())
    assert all(w.best_params == {"long": 1.0} for w in res.windows)
    agg = res.aggregate
    assert agg.param_stability == 1.0
    assert agg.n_configurations_tried == 2 * len(res.windows)
    assert agg.positive_window_fraction == 1.0
    assert agg.oos_total_return_mean > 0


def test_walk_forward_is_deterministic_and_json_safe() -> None:
    data = _regime_data(seed=5)
    cfg = WfaConfig(train_size=100, test_size=100, embargo=2, purge=1)
    a = run_walk_forward(data, _long_or_flat, {"long": [0.0, 1.0]}, cfg, BacktestConfig())
    b = run_walk_forward(data, _long_or_flat, {"long": [0.0, 1.0]}, cfg, BacktestConfig())
    da = json.dumps(a.to_dict(), allow_nan=False, sort_keys=True)
    assert da == json.dumps(b.to_dict(), allow_nan=False, sort_keys=True)


def test_walk_forward_efficiency() -> None:
    assert walk_forward_efficiency([1.0, 2.0], [0.5, 0.5]) == pytest.approx(1 / 3)
    assert walk_forward_efficiency([-1.0, 0.5], [1.0]) is None  # in-sample not positive
    assert walk_forward_efficiency([], []) is None


def test_bad_grid_and_objective_rejected() -> None:
    data = _regime_data()
    with pytest.raises(ConfigError):
        cfg = WfaConfig(train_size=50, test_size=50)
        run_walk_forward(data, _long_or_flat, {"long": []}, cfg, BacktestConfig())
    with pytest.raises(ConfigError):
        WfaConfig(train_size=50, test_size=50, objective="vibes")
