"""PTC calibration report generator - REFUSES to run without real data.

Usage (from ``agent-core``)::

    python -m backtesting.calibrate_ptc --dataset PATH/bars_1min.csv \\
        [--strategy-returns PATH/daily_returns.csv] [--floor-pct 1.0] --out-dir OUT

The tool computes empirical percentiles (with confidence intervals) of

* absolute 1-bar returns inside regular trading hours (input to the Aegis price collar),
* daily close-to-close losses and intraday drawdowns of the instruments, and, when
  ``--strategy-returns`` is supplied, of the strategy's own daily returns plus a seeded
  Monte Carlo of max drawdown,

and writes ``ptc_calibration_report.json`` and ``ptc_calibration_report.md``.

It never emits default or example numbers. It exits non-zero and writes nothing when

* ``--dataset`` is missing, unreadable, malformed or marked synthetic,
* the data span fewer than ``--min-trading-days`` trading days, or
* no symbol has enough observations for the highest percentile to rest on at least
  ``MIN_TAIL_OBSERVATIONS`` tail points.

The report is *candidate evidence for the owner*. Choosing an aggregator, a floor and a
safety margin, and signing off, are human decisions (see docs/regulatory/ptc-calibration.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import __version__
from .calibration import (
    MIN_TAIL_OBSERVATIONS,
    PERCENTILES,
    FloatArray,
    abs_bar_returns,
    daily_stats,
    min_observations,
    percentile_key,
    percentile_table,
)
from .data import read_bar_frame
from .models import BacktestError
from .montecarlo import BootstrapMethod, MonteCarloConfig, run_monte_carlo

REPORT_JSON = "ptc_calibration_report.json"
REPORT_MD = "ptc_calibration_report.md"
DISCLAIMER = (
    "Candidate evidence for owner review. This is not a compliance determination: thresholds "
    "must be chosen, margined and signed off by the firm, and regulatory statements require "
    "qualified legal review."
)
AGGREGATOR_NOTE = (
    "docs/regulatory/ptc-calibration.md does not state how per-symbol P99 values are "
    "aggregated across the universe. All three aggregators are shown; the owner must choose "
    "one and record the choice. No aggregator is selected by this tool."
)


class Refusal(BacktestError):
    """The tool declines to produce numbers (missing, short, synthetic or invalid data)."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _iso(ns: int) -> str:
    return pd.Timestamp(ns, unit="ns", tz="UTC").isoformat()


def _load_dataset(path_arg: str | None) -> tuple[Path, pd.DataFrame]:
    if not path_arg:
        raise Refusal(
            "--dataset is required. This tool computes calibration statistics only from a real "
            "dataset supplied by the owner and never emits default or example numbers."
        )
    path = Path(path_arg)
    frame, synthetic = read_bar_frame(path)
    if synthetic:
        raise Refusal(f"{path} is marked as synthetic data and cannot be used for calibration")
    return path, frame


def _collar_section(
    returns: dict[str, FloatArray], floor_pct: float | None, min_obs: int
) -> tuple[dict[str, Any], list[str]]:
    eligible = {s: r for s, r in returns.items() if r.shape[0] >= min_obs}
    excluded = {s: int(r.shape[0]) for s, r in returns.items() if s not in eligible}
    if not eligible:
        raise Refusal(
            f"no symbol has >= {min_obs} 1-bar return observations (required so the highest "
            f"percentile has at least {MIN_TAIL_OBSERVATIONS} tail observations); dataset too short"
        )
    pooled = np.concatenate(list(eligible.values()))
    per_symbol: dict[str, dict[str, Any]] = {
        s: {"n_observations": int(r.shape[0]), "percentiles": percentile_table(r)}
        for s, r in sorted(eligible.items())
    }
    p99 = percentile_key(0.99)
    sym_p99 = {s: v["percentiles"][p99]["value_pct"] for s, v in per_symbol.items()}
    pooled_p99 = float(np.quantile(pooled, 0.99)) * 100.0
    candidates = None
    if floor_pct is not None:
        candidates = {
            "floor_pct": floor_pct,
            "per_symbol": {s: max(floor_pct, v) for s, v in sym_p99.items()},
            "pooled_p99": max(floor_pct, pooled_p99),
            "worst_symbol": max(floor_pct, max(sym_p99.values())),
        }
    section = {
        "definition": "abs(close_t/close_{t-1}-1), consecutive RTH bars exactly bar_seconds apart",
        "min_observations_per_symbol": min_obs,
        "excluded_symbols_too_few_observations": excluded,
        "per_symbol": per_symbol,
        "pooled": {"n_observations": int(pooled.shape[0]), "percentiles": percentile_table(pooled)},
        "candidates_pct": candidates,
        "note": AGGREGATOR_NOTE,
    }
    return section, sorted(eligible)


def _daily_section(daily: pd.DataFrame, symbols: Sequence[str]) -> dict[str, Any]:
    sel = daily[daily["symbol"].isin(symbols)]
    out: dict[str, Any] = {
        "definition": "loss = -(last RTH close / previous available day's last RTH close - 1); "
        "instrument-level proxy, NOT strategy P&L",
        "per_symbol": {},
    }
    loss_all: list[FloatArray] = []
    for sym, grp in sel.groupby("symbol", sort=True):
        loss = (-grp["daily_return"].dropna()).to_numpy(dtype=np.float64)
        if loss.shape[0]:
            loss_all.append(loss)
            out["per_symbol"][str(sym)] = {
                "n_observations": int(loss.shape[0]),
                "percentiles": percentile_table(loss),
            }
    pooled = np.concatenate(loss_all) if loss_all else np.empty(0)
    out["pooled"] = (
        {"n_observations": int(pooled.shape[0]), "percentiles": percentile_table(pooled)}
        if pooled.shape[0]
        else None
    )
    intraday = sel["intraday_drawdown"].to_numpy(dtype=np.float64)
    out["intraday_drawdown_from_first_rth_close_pooled"] = (
        {"n_observations": int(intraday.shape[0]), "percentiles": percentile_table(intraday)}
        if intraday.shape[0]
        else None
    )
    return out


def _strategy_section(
    args: argparse.Namespace,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not args.strategy_returns:
        return None, None
    path = Path(args.strategy_returns)
    if not path.is_file():
        raise Refusal(f"strategy returns file not found: {path}")
    frame = pd.read_csv(path)
    if "return" not in frame.columns:
        raise Refusal("strategy returns CSV needs a 'return' column of daily fractional returns")
    rets = pd.to_numeric(frame["return"], errors="coerce").to_numpy(dtype=np.float64)
    if not np.isfinite(rets).all():
        raise Refusal("strategy returns contain non-numeric or non-finite values")
    if rets.shape[0] < args.min_trading_days:
        raise Refusal(
            f"strategy returns too short: {rets.shape[0]} rows < "
            f"{args.min_trading_days} trading days"
        )
    loss = {
        "source_sha256": _sha256(path),
        "n_observations": int(rets.shape[0]),
        "percentiles": percentile_table(-rets),
    }
    mc = run_monte_carlo(
        rets,
        MonteCarloConfig(
            seed=args.mc_seed, n_paths=args.mc_paths, block_size=args.mc_block_size,
            method=BootstrapMethod.BLOCK if args.mc_block_size > 1 else BootstrapMethod.IID,
            ruin_drawdown=args.ruin_drawdown,
        ),
    )  # fmt: skip
    return loss, mc.summary()


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    """Compute the full report dict, or raise :class:`Refusal` / a data error."""
    path, frame = _load_dataset(args.dataset)
    daily = daily_stats(frame)
    trading_days = int(daily["date"].nunique())
    if trading_days < args.min_trading_days:
        raise Refusal(
            f"dataset too short: {trading_days} trading days with regular-hours bars < "
            f"required {args.min_trading_days} (--min-trading-days)"
        )
    collar, symbols = _collar_section(
        abs_bar_returns(frame, args.bar_seconds), args.floor_pct, min_observations()
    )
    strategy_loss, mc = _strategy_section(args)
    return {
        "report_type": "ptc_calibration_candidate_evidence",
        "disclaimer": DISCLAIMER,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "tool": {
            "backtesting_version": __version__,
            "git_commit": _git_commit(),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },  # fmt: skip
        "dataset": {
            "path": str(path),
            "sha256": _sha256(path),
            "rows": int(len(frame)),
            "symbols": sorted(frame["symbol"].unique().tolist()),
            "symbols_used": symbols,
            "trading_days": trading_days,
            "first_ts_utc": _iso(int(frame["ts_ns"].min())),
            "last_ts_utc": _iso(int(frame["ts_ns"].max())),
            "bar_seconds": args.bar_seconds,
            "source_is_synthetic": False,
        },  # fmt: skip
        "parameters": {
            "percentiles": [percentile_key(q) for q in PERCENTILES],
            "min_trading_days": args.min_trading_days,
            "floor_pct": args.floor_pct,
            "mc_seed": args.mc_seed if args.strategy_returns else None,
        },  # fmt: skip
        "price_collar": collar,
        "daily_loss": _daily_section(daily, symbols),
        "strategy_daily_loss": strategy_loss,
        "monte_carlo": mc,
    }


def _fmt(est: dict[str, Any]) -> str:
    flag = "" if est["reliable"] else " (UNRELIABLE: <10 tail obs)"
    return f"{est['value_pct']:.4f}% [{est['ci_low_pct']:.4f}, {est['ci_high_pct']:.4f}]{flag}"


def _table(title: str, rows: dict[str, dict[str, Any]]) -> list[str]:
    keys = [percentile_key(q) for q in PERCENTILES]
    lines = [
        f"### {title}",
        "",
        "| Symbol | N | " + " | ".join(keys) + " |",
        "|---|---|" + "---|" * len(keys),
    ]
    for name, block in rows.items():
        cells = " | ".join(_fmt(block["percentiles"][k]) for k in keys)
        lines.append(f"| {name} | {block['n_observations']} | {cells} |")
    return [*lines, ""]


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable rendering of :func:`build_report` output."""
    ds = report["dataset"]
    lines = [
        "# PTC calibration report (candidate evidence)", "",
        f"> {report['disclaimer']}", "",
        f"- Generated (UTC): {report['generated_at_utc']}",
        f"- Dataset: `{ds['path']}` (sha256 `{ds['sha256']}`), {ds['rows']} rows",
        f"- Range: {ds['first_ts_utc']} to {ds['last_ts_utc']} ({ds['trading_days']} trading days)",
        f"- Symbols used: {', '.join(ds['symbols_used'])}",
        f"- Code commit: {report['tool']['git_commit']}", "",
        "Percentile cells: value [95% distribution-free CI]. Values are percent.", "",
        "## Price collar: absolute 1-bar returns (RTH)", "", report["price_collar"]["note"], "",
    ]  # fmt: skip
    collar = report["price_collar"]
    lines += _table("Per symbol", collar["per_symbol"])
    lines += _table("Pooled", {"pooled": collar["pooled"]})
    cand = collar["candidates_pct"]
    lines += ["### Candidate collars (percent; owner must choose)", ""]
    lines += ["Not computed (no --floor-pct supplied).", ""] if cand is None else [
        f"- floor: {cand['floor_pct']}", f"- pooled P99: {cand['pooled_p99']:.4f}",
        f"- worst-symbol P99: {cand['worst_symbol']:.4f}", "",
    ]  # fmt: skip
    daily = report["daily_loss"]
    lines += ["## Daily loss (instrument level, proxy only)", "", daily["definition"], ""]
    if daily["pooled"]:
        lines += _table("Pooled daily loss", {"pooled": daily["pooled"]})
    if report["strategy_daily_loss"]:
        lines += _table("Strategy daily loss", {"strategy": report["strategy_daily_loss"]})
    if report["monte_carlo"]:
        mc = report["monte_carlo"]
        dd = mc["max_drawdown"]
        lines += [
            "## Monte Carlo max drawdown (seeded bootstrap)", "",
            f"- paths {mc['n_paths']}, seed {mc['seed']}, method {mc['method']}, "
            f"block {mc['block_size']}",
            f"- max drawdown P95 {dd['p95']:.4%}, P99 {dd['p99']:.4%}, P99.9 {dd['p99.9']:.4%}",
            f"- P(ruin) with ruin drawdown {mc['ruin_drawdown']}: {mc['p_ruin']}", "",
        ]  # fmt: skip
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="PTC calibration report from REAL data (refuses otherwise)."
    )
    p.add_argument("--dataset", help="CSV/Parquet of intraday bars (see backtesting/data.py)")
    p.add_argument(
        "--strategy-returns", help="CSV with a 'return' column of daily fractional returns"
    )
    p.add_argument("--out-dir", default="calibration_out")
    p.add_argument("--bar-seconds", type=int, default=60)
    p.add_argument(
        "--floor-pct", type=float, default=None, help="collar floor in percent (optional)"
    )
    p.add_argument("--min-trading-days", type=int, default=250)
    p.add_argument("--mc-seed", type=int, default=0)
    p.add_argument("--mc-paths", type=int, default=50_000)
    p.add_argument("--mc-block-size", type=int, default=1)
    p.add_argument("--ruin-drawdown", type=float, default=None)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code (2 = refused)."""
    args = _parser().parse_args(argv)
    try:
        report = build_report(args)
    except BacktestError as exc:
        sys.stderr.write(f"REFUSING TO RUN: {exc}\n")
        return 2
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REPORT_JSON).write_text(
        json.dumps(report, indent=2, allow_nan=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / REPORT_MD).write_text(render_markdown(report), encoding="utf-8")
    sys.stdout.write(f"wrote {out_dir / REPORT_JSON} and {out_dir / REPORT_MD}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
