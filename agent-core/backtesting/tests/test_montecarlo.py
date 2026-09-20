"""Monte Carlo tests: analytic sanity checks, reproducibility. Inputs are SYNTHETIC/analytic."""

from __future__ import annotations

import json
import time
from decimal import Decimal as D

import numpy as np
import pytest

from backtesting.models import ClosedTrade, ConfigError
from backtesting.montecarlo import (
    BootstrapMethod,
    MonteCarloConfig,
    run_monte_carlo,
    trade_returns,
)


def cfg(**kw: object) -> MonteCarloConfig:
    base: dict[str, object] = {"n_paths": 2_000, "seed": 123}
    base.update(kw)
    return MonteCarloConfig(**base)  # type: ignore[arg-type]


def test_default_paths_is_50k() -> None:
    assert MonteCarloConfig(seed=1).n_paths == 50_000


@pytest.mark.parametrize("method", list(BootstrapMethod))
def test_zero_variance_returns_have_deterministic_outcome(method: BootstrapMethod) -> None:
    r = np.full(100, 0.01)
    res = run_monte_carlo(r, cfg(method=method, block_size=10))
    np.testing.assert_allclose(res.terminal_return, 1.01**100 - 1)
    np.testing.assert_allclose(res.max_drawdown, 0.0, atol=1e-12)


def test_all_loss_path_matches_closed_form() -> None:
    r = np.full(50, -0.02)
    res = run_monte_carlo(r, cfg())
    np.testing.assert_allclose(res.terminal_return, 0.98**50 - 1)
    np.testing.assert_allclose(res.max_drawdown, 1 - 0.98**50)
    s = res.summary()
    assert s["p_loss"] == 1.0


def test_scale_applies_before_compounding() -> None:
    res = run_monte_carlo(np.full(50, -0.02), cfg(scale=0.5))
    np.testing.assert_allclose(res.terminal_return, 0.99**50 - 1)


def test_same_seed_reproduces_and_different_seed_differs() -> None:
    rng = np.random.default_rng(0)  # only to build an input series
    r = rng.normal(0.001, 0.02, 80)
    a = run_monte_carlo(r, cfg())
    b = run_monte_carlo(r, cfg())
    c = run_monte_carlo(r, cfg(seed=124))
    np.testing.assert_array_equal(a.max_drawdown, b.max_drawdown)
    np.testing.assert_array_equal(a.terminal_return, b.terminal_return)
    assert not np.array_equal(a.terminal_return, c.terminal_return)
    assert a.summary() == b.summary()


def test_result_is_independent_of_chunking_internals_for_large_runs() -> None:
    r = np.random.default_rng(1).normal(0.0, 0.01, 30)
    big = run_monte_carlo(r, cfg(n_paths=12_000))  # spans several chunks
    again = run_monte_carlo(r, cfg(n_paths=12_000))
    np.testing.assert_array_equal(big.terminal_return, again.terminal_return)
    assert big.terminal_return.shape == (12_000,)


def test_single_step_two_point_distribution_is_fair() -> None:
    res = run_monte_carlo(np.array([0.01, -0.01]), cfg(n_paths=50_000, horizon=1))
    assert set(np.unique(np.round(res.terminal_return, 12))) == {-0.01, 0.01}
    p_up = float(np.mean(res.terminal_return > 0))
    assert abs(p_up - 0.5) < 4 * (0.25 / 50_000) ** 0.5 * 2  # 8 sigma of a fair coin


def test_mean_log_growth_matches_analytic_expectation() -> None:
    r = np.array([0.05, -0.03, 0.02, -0.01, 0.04])
    horizon, n = 40, 50_000
    res = run_monte_carlo(r, cfg(n_paths=n, horizon=horizon))
    logs = np.log1p(r)
    expected = horizon * logs.mean()
    se = np.sqrt(horizon) * logs.std() / np.sqrt(n)
    sample = np.log1p(res.terminal_return).mean()
    assert abs(sample - expected) < 5 * se


def test_block_bootstrap_full_length_block_is_a_rotation_of_the_history() -> None:
    r = np.random.default_rng(3).normal(0.0, 0.02, 25)
    res = run_monte_carlo(r, cfg(method=BootstrapMethod.BLOCK, block_size=25))
    np.testing.assert_allclose(res.terminal_return, np.prod(1 + r) - 1)  # rotation-invariant
    assert res.max_drawdown.std() > 0  # but the drawdown depends on the rotation


def test_ruin_probability_only_reported_when_defined() -> None:
    r = np.full(50, -0.02)  # drawdown 0.636 on every path
    assert run_monte_carlo(r, cfg()).summary()["p_ruin"] is None
    assert run_monte_carlo(r, cfg(ruin_drawdown=0.5)).summary()["p_ruin"] == 1.0
    assert run_monte_carlo(r, cfg(ruin_drawdown=0.7)).summary()["p_ruin"] == 0.0


def test_summary_percentiles_match_numpy_and_are_json_safe() -> None:
    r = np.random.default_rng(9).normal(0.002, 0.03, 60)
    res = run_monte_carlo(r, cfg(n_paths=5_000))
    s = res.summary()
    dd = s["max_drawdown"]
    assert dd["p95"] == pytest.approx(np.percentile(res.max_drawdown, 95))
    assert dd["p99.9"] == pytest.approx(np.percentile(res.max_drawdown, 99.9))
    assert dd["p95"] <= dd["p99"] <= dd["p99.9"]
    adverse = s["terminal_return_adverse"]
    assert adverse["conf_99"] == pytest.approx(np.percentile(res.terminal_return, 1))
    assert adverse["conf_99.9"] <= adverse["conf_99"] <= adverse["conf_95"]
    json.dumps(s, allow_nan=False)


@pytest.mark.parametrize(
    "returns, kw",
    [
        (np.array([0.1]), {}),  # too short
        (np.array([0.1, np.nan, 0.2]), {}),
        (np.array([0.1, -1.0, 0.2]), {}),  # total loss not representable
        (np.array([0.1, 0.2, 0.3]), {"n_paths": 0}),
        (np.array([0.1, 0.2, 0.3]), {"method": BootstrapMethod.BLOCK, "block_size": 9}),
        (np.array([0.1, 0.2, 0.3]), {"horizon": 0}),
        (np.array([0.1, 0.2, 0.3]), {"ruin_drawdown": 1.5}),
        (np.array([[0.1, 0.2], [0.3, 0.4]]), {}),
    ],
)
def test_invalid_inputs_are_rejected(returns: np.ndarray, kw: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        run_monte_carlo(returns, cfg(**kw))


def test_fifty_thousand_paths_run_quickly() -> None:
    r = np.random.default_rng(5).normal(0.001, 0.01, 250)
    start = time.perf_counter()
    res = run_monte_carlo(r, MonteCarloConfig(seed=7))
    elapsed = time.perf_counter() - start
    assert res.terminal_return.shape == (50_000,)
    assert elapsed < 30.0  # generous CI bound; the real timing is reported separately


def test_trade_returns_from_ledger() -> None:
    t = ClosedTrade("A", "LONG", 0, 1, D(10), D(100), D(110), D(100), D(0))
    lost = ClosedTrade("A", "LONG", 0, 1, D(10), D(100), D(95), D(-50), D(0))
    np.testing.assert_allclose(trade_returns([t, lost]), [0.1, -0.05])
