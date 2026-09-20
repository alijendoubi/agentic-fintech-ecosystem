"""Metric tests against hand-computed values on tiny curves (numbers are illustrative)."""

from __future__ import annotations

import math
from decimal import Decimal as D

import numpy as np
import pytest

from backtesting.metrics import MetricsConfig, compute_metrics, max_drawdown, period_returns
from backtesting.models import ClosedTrade, ConfigError


def trade(net: str) -> ClosedTrade:
    gross = D(net)
    return ClosedTrade("A", "LONG", 0, 1, D("1"), D("100"), D("100") + gross, gross, D("0"))


def test_period_returns_and_total_return() -> None:
    eq = np.array([100.0, 110.0, 99.0])
    np.testing.assert_allclose(period_returns(eq), [0.1, -0.1])
    cfg = MetricsConfig(periods_per_year=1.0)
    m = compute_metrics(np.arange(3), eq, np.zeros(3), [], D(0), cfg)
    assert m.total_return == pytest.approx(-0.01)
    # annualised with ppy=1 over 2 periods: (0.99)^(1/2)-1
    assert m.annualised_return == pytest.approx(math.sqrt(0.99) - 1)


def test_sharpe_convention_is_mean_over_sample_std_annualised() -> None:
    eq = np.array([100.0, 101.0, 100.0, 102.0, 103.0])
    r = np.diff(eq) / eq[:-1]
    cfg = MetricsConfig(periods_per_year=252.0)
    m = compute_metrics(np.arange(5), eq, np.zeros(5), [], D(0), cfg)
    expected = r.mean() / r.std(ddof=1) * math.sqrt(252.0)
    assert m.sharpe == pytest.approx(expected)
    assert m.annualised_volatility == pytest.approx(r.std(ddof=1) * math.sqrt(252.0))


def test_risk_free_rate_reduces_sharpe() -> None:
    eq = np.array([100.0, 101.0, 100.5, 102.0, 103.0])
    base = compute_metrics(np.arange(5), eq, np.zeros(5), [], D(0), MetricsConfig(252.0, 0.0))
    withrf = compute_metrics(np.arange(5), eq, np.zeros(5), [], D(0), MetricsConfig(252.0, 0.05))
    assert base.sharpe is not None and withrf.sharpe is not None
    assert withrf.sharpe < base.sharpe


def test_zero_variance_gives_undefined_sharpe_not_inf() -> None:
    eq = np.full(6, 100.0)
    m = compute_metrics(np.arange(6), eq, np.zeros(6), [], D(0), MetricsConfig())
    assert m.sharpe is None and m.sortino is None and m.max_drawdown == 0.0


def test_max_drawdown_and_duration() -> None:
    #            peak      trough                  recovers
    eq = np.array([100.0, 120.0, 90.0, 95.0, 100.0, 120.0, 130.0])
    dd, periods, dur_ns = max_drawdown(eq, np.arange(7) * 10)
    assert dd == pytest.approx(0.25)  # (120-90)/120
    assert periods == 4  # peak at index 1, recovery at index 5
    assert dur_ns == 40  # ts[5]-ts[1]
    assert compute_metrics(
        np.arange(7) * 10, eq, np.zeros(7), [], D(0), MetricsConfig()
    ).calmar is not None


def test_drawdown_never_recovered_runs_to_the_end() -> None:
    eq = np.array([100.0, 90.0, 80.0, 85.0])
    dd, periods, dur_ns = max_drawdown(eq, np.array([0, 1, 2, 3]))
    assert dd == pytest.approx(0.2) and periods == 3 and dur_ns == 3


def test_trade_statistics() -> None:
    trades = [trade("10"), trade("-5"), trade("20"), trade("-10")]
    eq = np.array([100.0, 101.0, 102.0])
    m = compute_metrics(np.arange(3), eq, np.zeros(3), trades, D(0), MetricsConfig())
    assert m.n_trades == 4
    assert m.hit_rate == pytest.approx(0.5)
    assert m.profit_factor == pytest.approx(30.0 / 15.0)


def test_profit_factor_undefined_without_losses_and_hit_rate_without_trades() -> None:
    eq = np.array([100.0, 101.0, 102.0])
    only_wins = compute_metrics(
        np.arange(3), eq, np.zeros(3), [trade("1")], D(0), MetricsConfig()
    )
    assert only_wins.profit_factor is None and only_wins.hit_rate == 1.0
    none = compute_metrics(np.arange(3), eq, np.zeros(3), [], D(0), MetricsConfig())
    assert none.hit_rate is None and none.profit_factor is None and none.n_trades == 0


def test_turnover_and_exposure() -> None:
    eq = np.array([100.0, 100.0, 100.0, 100.0])
    gross = np.array([0.0, 50.0, 50.0, 0.0])
    m = compute_metrics(np.arange(4), eq, gross, [], D("300"), MetricsConfig())
    assert m.turnover == pytest.approx(3.0)  # 300 traded / 100 mean equity
    assert m.exposure == pytest.approx(0.5)
    assert m.avg_gross_exposure == pytest.approx(0.25)


def test_input_validation() -> None:
    with pytest.raises(ConfigError):
        compute_metrics(np.arange(1), np.array([1.0]), np.zeros(1), [], D(0), MetricsConfig())
    with pytest.raises(ConfigError):
        MetricsConfig(periods_per_year=0.0)
    with pytest.raises(ConfigError):
        compute_metrics(np.arange(2), np.array([1.0, 0.0]), np.zeros(2), [], D(0), MetricsConfig())


def test_to_dict_is_json_safe() -> None:
    import json

    eq = np.array([100.0, 101.0, 100.0, 102.0])
    m = compute_metrics(np.arange(4), eq, np.zeros(4), [trade("1")], D(0), MetricsConfig())
    json.dumps(m.to_dict(), allow_nan=False)
