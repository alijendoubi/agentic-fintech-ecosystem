"""PTC calibration machinery tests.

The datasets below are SYNTHETIC, generated in-test with analytically known return
structure, and are never written outside ``tmp_path``. Nothing here is a calibration result.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtesting.calibrate_ptc import main
from backtesting.calibration import percentile_with_ci
from backtesting.testing.synthetic import generate_market_data, write_synthetic_csv

BARS_PER_DAY = 390
JUMP_BAR = 100


def make_minute_csv(path: Path, symbols: list[str], days: int) -> None:
    """1-minute RTH bars (close-stamped 09:31..16:00 ET). |return| = 0.001 except one 0.02 jump."""
    frames = []
    day_index = pd.bdate_range("2024-01-02", periods=days)
    for sym in symbols:
        price = 100.0
        rows: list[tuple[int, str, float, float, float, float, float]] = []
        for day in day_index:
            stamps = pd.date_range(
                pd.Timestamp(f"{day.date()} 09:31", tz="America/New_York"),
                periods=BARS_PER_DAY,
                freq="min",
            ).tz_convert("UTC")
            for i, ts in enumerate(stamps):
                if i == 0:
                    ret = 0.0 if not rows else 0.05  # overnight gap: must be ignored
                else:
                    ret = 0.02 if i == JUMP_BAR else (0.001 if i % 2 else -0.001)
                new = price * (1.0 + ret)
                rows.append((ts.value, sym, price, max(price, new), min(price, new), new, 1000.0))
                price = new
        frames.append(
            pd.DataFrame(
                rows, columns=["timestamp_ns", "symbol", "open", "high", "low", "close", "volume"]
            )
        )
    pd.concat(frames).to_csv(path, index=False)


def run(args: list[str]) -> int:
    return main(args)


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cal") / "bars.csv"
    make_minute_csv(path, ["AAA", "BBB"], days=30)
    return path


def test_refuses_without_dataset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run(["--out-dir", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert code == 2 and "REFUSING" in err and "--dataset" in err
    assert not (tmp_path / "out").exists()  # nothing emitted


def test_refuses_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = run(["--dataset", str(tmp_path / "nope.csv"), "--out-dir", str(tmp_path / "out")])
    assert code == 2 and "REFUSING" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_refuses_dataset_with_too_few_trading_days(
    dataset: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["--dataset", str(dataset), "--out-dir", str(tmp_path / "o")])  # default guard
    err = capsys.readouterr().err
    assert code == 2 and "too short" in err and "trading days" in err
    assert not (tmp_path / "o").exists()


def test_refuses_when_no_symbol_has_enough_observations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    small = tmp_path / "small.csv"
    make_minute_csv(small, ["AAA"], days=3)  # ~1,167 returns < 10,000
    code = run(
        ["--dataset", str(small), "--min-trading-days", "1", "--out-dir", str(tmp_path / "o")]
    )
    assert code == 2 and "observations" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_refuses_synthetic_marked_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "synthetic.csv"
    write_synthetic_csv(path, generate_market_data(["AAA"], 300, seed=1))
    code = run(
        ["--dataset", str(path), "--min-trading-days", "1", "--out-dir", str(tmp_path / "o")]
    )
    assert code == 2 and "synthetic" in capsys.readouterr().err.lower()


def test_report_matches_analytic_structure(dataset: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = run(
        [
            "--dataset",
            str(dataset),
            "--min-trading-days",
            "20",
            "--out-dir",
            str(out),
            "--floor-pct",
            "1.0",
        ]
    )
    assert code == 0
    report = json.loads((out / "ptc_calibration_report.json").read_text(encoding="utf-8"))
    md = (out / "ptc_calibration_report.md").read_text(encoding="utf-8")
    assert "owner" in md.lower() and "not a compliance determination" in md.lower()
    ds = report["dataset"]
    assert ds["sha256"] == hashlib.sha256(dataset.read_bytes()).hexdigest()
    assert ds["trading_days"] == 30 and ds["symbols"] == ["AAA", "BBB"]
    aaa = report["price_collar"]["per_symbol"]["AAA"]
    # 389 in-day returns/day x 30 days; the overnight jump is never a 1-minute return.
    assert aaa["n_observations"] == 389 * 30
    # 1 of 389 returns per day is a 2% jump (0.26% of obs): P99 stays at 0.1%, P99.9 is the jump.
    assert aaa["percentiles"]["p99"]["value_pct"] == pytest.approx(0.1, abs=1e-6)
    assert aaa["percentiles"]["p99.9"]["value_pct"] == pytest.approx(2.0, abs=1e-6)
    assert aaa["percentiles"]["p99.9"]["reliable"] is True  # >= 10 tail observations
    cand = report["price_collar"]["candidates_pct"]
    assert cand["floor_pct"] == 1.0 and cand["pooled_p99"] == pytest.approx(1.0)
    assert "aggregator" in report["price_collar"]["note"].lower()
    daily = report["daily_loss"]["pooled"]["percentiles"]
    assert daily["p99.9"]["reliable"] is False  # 58 obs: decided by < 10 observations
    assert report["monte_carlo"] is None  # no strategy returns supplied


def test_collar_candidates_not_computed_without_floor(dataset: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run(["--dataset", str(dataset), "--min-trading-days", "20", "--out-dir", str(out)]) == 0
    report = json.loads((out / "ptc_calibration_report.json").read_text(encoding="utf-8"))
    assert report["price_collar"]["candidates_pct"] is None


def test_strategy_returns_add_reproducible_monte_carlo(dataset: Path, tmp_path: Path) -> None:
    rets = tmp_path / "daily.csv"
    rng = np.random.default_rng(4)  # test-only series
    pd.DataFrame(
        {"date": pd.bdate_range("2023-01-02", periods=300), "return": rng.normal(0.0, 0.01, 300)}
    ).to_csv(rets, index=False)
    outs = []
    for name in ("a", "b"):
        out = tmp_path / name
        code = run(
            [
                "--dataset",
                str(dataset),
                "--min-trading-days",
                "20",
                "--strategy-returns",
                str(rets),
                "--mc-seed",
                "11",
                "--mc-paths",
                "2000",
                "--ruin-drawdown",
                "0.2",
                "--out-dir",
                str(out),
            ]
        )
        assert code == 0
        outs.append(json.loads((out / "ptc_calibration_report.json").read_text(encoding="utf-8")))
    mc_a, mc_b = outs[0]["monte_carlo"], outs[1]["monte_carlo"]
    assert mc_a == mc_b and mc_a["seed"] == 11 and mc_a["n_paths"] == 2000
    assert mc_a["ruin_drawdown"] == 0.2 and mc_a["p_ruin"] is not None
    assert outs[0]["strategy_daily_loss"]["n_observations"] == 300


def test_percentile_with_ci_is_distribution_free() -> None:
    x = np.arange(1, 1001, dtype=float)
    est = percentile_with_ci(x, 0.5)
    assert est.ci_low <= est.value <= est.ci_high
    assert est.ci_low < 500 < est.ci_high and est.n_obs == 1000 and est.reliable
    tail = percentile_with_ci(x, 0.999)
    assert tail.n_tail == pytest.approx(1.0) and not tail.reliable
    assert tail.ci_truncated  # upper rank falls beyond the sample: flagged, not hidden
    with pytest.raises(ValueError):
        percentile_with_ci(np.array([]), 0.5)
