# backtesting

Event-driven backtest engine, regime-aware walk-forward analysis (WFA), seeded Monte Carlo,
and the *machinery* for PTC calibration. Tracks ALI-50 (machinery only), ALI-51, ALI-52.

> **No result in this repository was produced by this package.** There is no market-data
> dataset in the repo. All test data is synthetic, generated inside the tests, and marked as
> such. Every threshold in `docs/regulatory/ptc-calibration.md` still needs real data.

## Layout

| Module | Purpose |
|---|---|
| `models.py` | Immutable value types; `RegimeLabel`/`Side` mirror `market_snapshot.proto` / `trade_signal.proto` |
| `data.py` | `MarketData`, CSV/Parquet loaders, validation, as-of regime attachment |
| `feed.py` | Point-in-time feed: strategies get `MarketView` objects, never the raw arrays |
| `portfolio.py` | Decimal accounting (cash, positions, average cost, realised/unrealised P&L, fees), invariants |
| `execution.py` | Costs (commission, half-spread, linear/sqrt impact), latency, participation cap, collar |
| `engine.py` | `run_backtest(data, strategy, config)`; `python -m backtesting.engine` smoke CLI |
| `metrics.py` | Return/vol/Sharpe/Sortino/Calmar, drawdown + duration, hit rate, profit factor, turnover, exposure |
| `wfa.py`, `regime_analysis.py` | Rolling/anchored WFA with purge + embargo; per-window and per-regime stats |
| `montecarlo.py` | Seeded iid / block bootstrap, 50,000 paths by default |
| `calibration.py`, `calibrate_ptc.py` | Empirical collar/drawdown percentiles; refuses without a real dataset |
| `testing/synthetic.py` | **Synthetic** data generator, for tests only |

## Setup and tests

```bash
# from the repo root, in your own venv (never inside the repo)
python -m pip install -r agent-core/backtesting/requirements-dev.txt
cd agent-core
python -m pytest backtesting -c backtesting/pytest.ini --cov=backtesting --cov-config=backtesting/.coveragerc
python -m ruff check backtesting
python -m mypy --config-file backtesting/mypy.ini backtesting
```

## Data format

CSV (or Parquet with `pyarrow`): `timestamp,symbol,open,high,low,close,volume[,regime]`.
`timestamp` is ISO-8601 with a UTC offset, or integer ns since epoch when the column is named
`timestamp_ns`. **Timestamps are bar close times** (the moment the bar is observable). Naive
timestamps are rejected. `regime` uses the proto names (`TRENDING_BULL`, `TRENDING_BEAR`,
`HIGH_VOL_CHOP`, `LOW_VOL_CHOP`, `CRISIS`); a separate label series can be attached with
`MarketData.with_regimes(ts, labels)` (as-of join, never forward-looking).

## Engine semantics (short)

* A strategy is `Callable[[MarketView, PortfolioSnapshot], Sequence[OrderIntent]]`. It sees
  only bars with `timestamp <= now`; asking for the future raises `LookaheadError`.
* Orders are filled at the first event strictly after the decision bar and at/after
  `decision + latency (+ seeded jitter)`. Never on the signal's own bar.
* Fill price = fill-bar close (default) or open, then `ref * (1 +/- (half_spread + impact))`.
  Impact coefficients are inputs and must be fitted from data; none are assumed here.
* Volume participation cap gives partial fills that carry to later bars; a price outside
  `collar_bps` of the decision price rejects the order; limits wait until executable or expiry.
* `omega_threshold` mirrors the Judge abstain rule (intents below it are recorded, not traded).
* Metric conventions (annualisation, Sharpe, drawdown duration, profit factor) are documented
  at the top of `metrics.py`. `periods_per_year` is explicit, never inferred.
* Deterministic: same data, strategy, config and seed give the same `result.fingerprint()`.

## Walk-forward

`run_walk_forward(data, factory, param_grid, WfaConfig(...), BacktestConfig(...))`. Test windows
start `purge + embargo` bars after the (purged) end of training; parameters are chosen on the
training slice only; unobserved regimes are reported as `insufficient_evidence`, never dropped
or passed. The number of configurations tried is reported for multiple-testing adjustment
(no deflated Sharpe is computed here). Pass thresholds do not exist in the repo and are not
invented.

## Monte Carlo

`run_monte_carlo(returns, MonteCarloConfig(seed=...))` bootstraps trade or period returns
(fractions, e.g. `0.01`) into equity paths and reports max-drawdown and terminal-return
percentiles (P95/P99/P99.9). `p_ruin` is computed only if you pass an explicit
`ruin_drawdown`; the repo does not define ruin.

## PTC calibration: how the owner runs it on real data

1. Obtain intraday bars (1-minute for the collar procedure) from the data vendor and save as
   CSV in the format above. Check the vendor licence permits local storage. The data file
   must not carry the `# synthetic-data` marker.
2. Optionally produce a daily strategy-return CSV (`date,return`, fractional) from a real
   backtest or paper-trading run of the actual strategy, e.g. with
   `np.savetxt`/pandas on `metrics.period_returns(result.equity.equity)`.
3. From `agent-core`:

   ```bash
   python -m backtesting.calibrate_ptc --dataset /data/bars_1min.csv \
       --strategy-returns /data/strategy_daily.csv --floor-pct 1.0 \
       --mc-seed 20260101 --ruin-drawdown <owner-defined> --out-dir calibration_out
   ```

   (`--floor-pct 1.0` is the floor named in `ptc-calibration.md`; omit it and no candidate
   collar is computed. `--ruin-drawdown` is your definition of ruin; omit it and `p_ruin` is
   `null`.)
4. The tool **exits 2 and writes nothing** if `--dataset` is missing/unreadable/synthetic,
   the data span fewer than `--min-trading-days` (default 250) regular-hours days, or no symbol
   has enough 1-bar observations (10,000: the highest percentile P99.9 then rests on >= 10
   tail points). It never prints example or default numbers.
5. Outputs: `ptc_calibration_report.json` (machine-readable) and `.md`, each with the dataset
   SHA-256, row count, date range, code commit, seed, percentiles with 95% distribution-free
   confidence intervals, and a `reliable` flag (false when < 10 observations lie in the tail).
6. Feeding `docs/regulatory/ptc-calibration.md`: a human reads the report, chooses the
   aggregator (per-symbol, pooled or worst-symbol; the doc does not say which), the floor and a
   safety margin, then records in that document the chosen value **together with** the report
   filename, dataset SHA-256, code commit, seed and the person who chose it. Do not paste
   numbers without that provenance. Unreliable percentiles (notably a 5-year daily P99.9,
   about one observation) must be reported as such, not as a point estimate.

Not covered by this tooling: order-size limit (needs fill data to fit impact), unusual-order
P95 (needs order history), regime-gate and omega thresholds (need the strategy/Judge outputs).
The daily-loss section is an *instrument-level proxy*, not strategy P&L.
