"""Reference strategies and the engine CLI (synthetic data written to tmp_path)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backtesting.engine import BacktestConfig, main, run_backtest
from backtesting.strategies import buy_and_hold, sma_crossover
from backtesting.testing.synthetic import generate_market_data, write_synthetic_csv


def test_buy_and_hold_buys_once() -> None:
    data = generate_market_data(["AAA"], 50, seed=3)
    res = run_backtest(data, buy_and_hold("AAA"), BacktestConfig())
    assert len(res.fills) == 1 and res.fills[0].timestamp_ns > res.fills[0].signal_ts_ns


def test_sma_crossover_trades_and_validates_windows() -> None:
    data = generate_market_data(["AAA"], 400, seed=4)
    res = run_backtest(data, sma_crossover("AAA", 5, 20), BacktestConfig())
    assert len(res.trades) >= 1
    with pytest.raises(ValueError):
        sma_crossover("AAA", 20, 5)


def test_multi_symbol_run_and_regime_symbol() -> None:
    data = generate_market_data(["AAA", "BBB"], 60, seed=8)
    res = run_backtest(data, buy_and_hold("BBB"), BacktestConfig(regime_symbol="BBB"))
    assert res.equity.equity.shape[0] == 60 and res.fills[0].symbol == "BBB"


def test_cli_prints_metrics_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "bars.csv"
    write_synthetic_csv(path, generate_market_data(["AAA"], 80, seed=1))
    code = main(["--data", str(path), "--symbol", "AAA", "--strategy", "buy_and_hold"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and len(out["fingerprint"]) == 64 and "sharpe" in out["metrics"]


def test_cli_reports_bad_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--data", str(tmp_path / "missing.csv"), "--symbol", "A", "--strategy", "sma_crossover"]
    code = main(args)
    assert code == 2 and "error:" in capsys.readouterr().err
