"""Event-driven, deterministic backtest engine.

Per simulated instant ``t`` (one per timestamp on the merged timeline) the engine

1. reveals the bars stamped ``t`` (marks update);
2. tries to fill pending orders that are *eligible* (created strictly before ``t`` and
   ``t >= created + latency``) against the bars of ``t`` - so an order is never filled on
   the bar that produced the signal;
3. records the equity point (post-fill, marked at ``t``'s close) and checks invariants;
4. calls the strategy with a point-in-time :class:`MarketView` and a portfolio snapshot,
   and queues the returned :class:`OrderIntent` objects.

Run as a module for a quick smoke test on a CSV::

    python -m backtesting.engine --data bars.csv --symbol AAA --strategy buy_and_hold
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np
from numpy.typing import NDArray

from .data import MarketData, load_market_data
from .execution import (
    CostModel,
    ExecutionConfig,
    PendingOrder,
    attempt_fill,
    to_decimal,
)
from .feed import FeedEvent, MarketView, PointInTimeFeed
from .metrics import Metrics, MetricsConfig, compute_metrics
from .models import (
    Bar,
    ClosedTrade,
    ConfigError,
    DataValidationError,
    Fill,
    OrderIntent,
    RegimeLabel,
    Rejection,
)
from .portfolio import Portfolio, PortfolioSnapshot

Strategy = Callable[[MarketView, PortfolioSnapshot], Sequence[OrderIntent]]


@dataclass(frozen=True)
class BacktestConfig:
    """Everything that defines a run besides the data and the strategy."""

    initial_cash: Decimal = Decimal(100_000)
    cost: CostModel = field(default_factory=CostModel)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    seed: int = 0
    trading_start_ts_ns: int | None = None
    regime_symbol: str | None = None
    check_invariants: bool = True

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ConfigError("initial_cash must be positive")


@dataclass(frozen=True)
class EquityCurve:
    """Mark-to-market equity sampled at every simulated event (read-only arrays)."""

    ts_ns: NDArray[np.int64]
    equity: NDArray[np.float64]
    gross_exposure: NDArray[np.float64]
    regime: NDArray[np.int8]


@dataclass(frozen=True)
class BacktestResult:
    """Immutable outcome of one run."""

    config: BacktestConfig
    equity: EquityCurve
    fills: tuple[Fill, ...]
    trades: tuple[ClosedTrade, ...]
    rejections: tuple[Rejection, ...]
    abstained: tuple[OrderIntent, ...]
    open_orders: tuple[PendingOrder, ...]
    final_portfolio: Portfolio
    final_equity: Decimal
    metrics: Metrics

    def fingerprint(self) -> str:
        """SHA-256 over fills, rejections and the equity curve: identical runs match."""
        h = hashlib.sha256()
        for f in self.fills:
            h.update(
                f"{f.order_id}|{f.symbol}|{int(f.side)}|{f.quantity}|{f.price}|{f.fee}|"
                f"{f.timestamp_ns}\n".encode()
            )
        for r in self.rejections:
            h.update(f"R|{r.order_id}|{r.timestamp_ns}|{r.reason}\n".encode())
        h.update(self.equity.ts_ns.tobytes())
        h.update(self.equity.equity.tobytes())
        return h.hexdigest()


@dataclass
class _RunState:
    """Mutable loop state; never escapes :func:`run_backtest`."""

    portfolio: Portfolio
    marks: dict[str, Decimal] = field(default_factory=dict)
    pending: list[PendingOrder] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    abstained: list[OrderIntent] = field(default_factory=list)
    next_order_id: int = 1


def _event_regime(ev: FeedEvent, regime_symbol: str | None) -> int:
    if regime_symbol is not None:
        bar = ev.new_bars.get(regime_symbol)
        return int(bar.regime) if bar is not None else int(RegimeLabel.REGIME_UNKNOWN)
    if not ev.new_bars:
        return int(RegimeLabel.REGIME_UNKNOWN)
    return int(ev.new_bars[min(ev.new_bars)].regime)


def _process_pending(state: _RunState, ev: FeedEvent, cfg: BacktestConfig) -> None:
    """Fill / expire / reject pending orders at this event, FIFO."""
    capacity: dict[str, Decimal] = {}
    still_open: list[PendingOrder] = []
    for order in state.pending:
        sym = order.intent.symbol
        bar = ev.new_bars.get(sym)
        if order.expires_ts_ns is not None and ev.ts_ns > order.expires_ts_ns:
            state.rejections.append(
                Rejection(order.order_id, sym, ev.ts_ns, "expired", order.remaining)
            )
            continue
        if bar is None or ev.ts_ns <= order.created_ts_ns or ev.ts_ns < order.eligible_ts_ns:
            still_open.append(order)
            continue
        if sym not in capacity:
            cap = int(bar.volume * cfg.execution.participation_cap)
            capacity[sym] = Decimal(cap)
        pos = state.portfolio.positions.get(sym)
        closes = ev.view.history(sym).closes(cfg.cost.vol_lookback + 1)
        attempt = attempt_fill(
            order, bar, closes, capacity[sym],
            pos.quantity if pos else Decimal(0), state.portfolio.cash,
            cfg.cost, cfg.execution,
        )  # fmt: skip
        if attempt.rejection is not None:
            state.rejections.append(attempt.rejection)
        if attempt.fill is not None:
            capacity[sym] -= attempt.fill.quantity
            state.portfolio, closed = state.portfolio.apply_fill(attempt.fill)
            state.fills.append(attempt.fill)
            state.trades.extend(closed)
        if attempt.order is not None:
            still_open.append(attempt.order)
    state.pending = still_open


def _enqueue(
    state: _RunState,
    intents: Sequence[OrderIntent],
    ev: FeedEvent,
    cfg: BacktestConfig,
    rng: np.random.Generator,
) -> None:
    ex = cfg.execution
    for intent in intents:
        if not isinstance(intent, OrderIntent):
            raise TypeError(f"strategy must return OrderIntent objects, got {type(intent)!r}")
        latest = ev.view.history(intent.symbol).latest  # unknown symbol raises
        if ex.omega_threshold is not None and (
            intent.omega is not None and intent.omega < ex.omega_threshold
        ):
            state.abstained.append(intent)
            continue
        oid = state.next_order_id
        state.next_order_id += 1
        if latest is None:
            state.rejections.append(
                Rejection(oid, intent.symbol, ev.ts_ns, "no_market_data", intent.quantity)
            )
            continue
        jitter = int(rng.integers(0, ex.latency_jitter_ns + 1)) if ex.latency_jitter_ns else 0
        ttl = intent.valid_for_ns if intent.valid_for_ns is not None else ex.default_ttl_ns
        state.pending.append(
            PendingOrder(
                oid, intent, ev.ts_ns, ev.ts_ns + ex.latency_ns + jitter,
                None if ttl is None else ev.ts_ns + ttl,
                latest.close, intent.quantity, latest.regime,
            )
        )  # fmt: skip


def _update_marks(marks: dict[str, Decimal], new_bars: Mapping[str, Bar]) -> None:
    for sym, bar in new_bars.items():
        marks[sym] = to_decimal(bar.close)


def run_backtest(data: MarketData, strategy: Strategy, config: BacktestConfig) -> BacktestResult:
    """Run ``strategy`` over ``data`` and return an immutable :class:`BacktestResult`."""
    rng = np.random.default_rng(config.seed)
    state = _RunState(Portfolio.new(config.initial_cash))
    start = config.trading_start_ts_ns
    ts_l: list[int] = []
    eq_l: list[float] = []
    gross_l: list[float] = []
    reg_l: list[int] = []
    for ev in PointInTimeFeed(data).events():
        _update_marks(state.marks, ev.new_bars)
        if start is not None and ev.ts_ns < start:
            continue
        _process_pending(state, ev, config)
        if config.check_invariants:
            state.portfolio.check_invariants(state.marks)
        ts_l.append(ev.ts_ns)
        eq_l.append(float(state.portfolio.equity(state.marks)))
        gross_l.append(float(state.portfolio.gross_exposure(state.marks)))
        reg_l.append(_event_regime(ev, config.regime_symbol))
        snap = state.portfolio.snapshot(ev.ts_ns, state.marks)
        _enqueue(state, strategy(ev.view, snap), ev, config, rng)
    return _finish(state, config, ts_l, eq_l, gross_l, reg_l)


def _finish(
    state: _RunState,
    config: BacktestConfig,
    ts_l: list[int],
    eq_l: list[float],
    gross_l: list[float],
    reg_l: list[int],
) -> BacktestResult:
    curve = EquityCurve(
        np.asarray(ts_l, dtype=np.int64),
        np.asarray(eq_l, dtype=np.float64),
        np.asarray(gross_l, dtype=np.float64),
        np.asarray(reg_l, dtype=np.int8),
    )
    for arr in (curve.ts_ns, curve.equity, curve.gross_exposure, curve.regime):
        arr.setflags(write=False)
    metrics = compute_metrics(
        curve.ts_ns, curve.equity, curve.gross_exposure, state.trades,
        state.portfolio.traded_notional, config.metrics,
    )  # fmt: skip
    return BacktestResult(
        config, curve, tuple(state.fills), tuple(state.trades), tuple(state.rejections),
        tuple(state.abstained), tuple(state.pending), state.portfolio,
        state.portfolio.equity(state.marks), metrics,
    )  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    """Tiny CLI: run a reference strategy on a CSV/Parquet file and print metrics as JSON."""
    from .strategies import REFERENCE_STRATEGIES

    parser = argparse.ArgumentParser(description="Backtest a reference strategy on bar data.")
    parser.add_argument("--data", required=True, help="CSV/Parquet bars (see backtesting/data.py)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--strategy", choices=sorted(REFERENCE_STRATEGIES), required=True)
    parser.add_argument("--periods-per-year", type=float, default=252.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        data = load_market_data(args.data)
        cfg = BacktestConfig(seed=args.seed, metrics=MetricsConfig(args.periods_per_year))
        result = run_backtest(data, REFERENCE_STRATEGIES[args.strategy](args.symbol), cfg)
    except (DataValidationError, ConfigError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps(
            {"fingerprint": result.fingerprint(), "metrics": result.metrics.to_dict()},
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
