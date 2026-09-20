"""Engine tests: look-ahead, latency, costs, partial fills, collar, determinism. SYNTHETIC data."""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal as D

import numpy as np
import pytest

from backtesting.data import MarketData, SymbolSeries
from backtesting.engine import BacktestConfig, Strategy, run_backtest
from backtesting.execution import CostModel, ExecutionConfig, FillPrice, ImpactModel
from backtesting.feed import MarketView
from backtesting.models import (
    ConfigError,
    DataValidationError,
    LookaheadError,
    OrderIntent,
    RegimeLabel,
    Side,
)
from backtesting.portfolio import PortfolioSnapshot
from backtesting.testing.synthetic import generate_market_data

BAR_NS = 10


def mk(
    closes: Sequence[float],
    volume: float = 1000.0,
    opens: Sequence[float] | None = None,
    symbol: str = "AAA",
) -> MarketData:
    n = len(closes)
    op = list(opens) if opens is not None else list(closes)
    hi = [max(o, c) for o, c in zip(op, closes, strict=True)]
    lo = [min(o, c) for o, c in zip(op, closes, strict=True)]
    ts = [BAR_NS * (i + 1) for i in range(n)]
    return MarketData([SymbolSeries.create(symbol, ts, op, hi, lo, closes, [volume] * n)])


def once(intent: OrderIntent, at_call: int = 0) -> Strategy:
    calls = {"n": 0}

    def strat(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        calls["n"] += 1
        return [intent] if calls["n"] == at_call + 1 else []

    return strat


def buy(qty: float = 10, **kw: object) -> OrderIntent:
    return OrderIntent.of("AAA", Side.BUY, qty, **kw)  # type: ignore[arg-type]


def cfg(**exec_kw: object) -> BacktestConfig:
    return BacktestConfig(
        initial_cash=D("100000"), execution=ExecutionConfig(**exec_kw)  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------- look-ahead


def test_strategy_peeking_at_the_future_raises() -> None:
    def peeker(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        hist = view.history("AAA")
        _ = hist[len(hist)]  # the next bar: not yet observable
        return []

    with pytest.raises(LookaheadError):
        run_backtest(mk([100, 101, 102]), peeker, cfg())


def test_strategy_only_ever_sees_bars_up_to_now() -> None:
    seen: list[tuple[int, int, int]] = []

    def recorder(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        h = view.history("AAA")
        seen.append((view.now_ns, len(h), int(h.timestamps()[-1])))
        return []

    run_backtest(mk([100, 101, 102, 103]), recorder, cfg())
    assert seen == [(10, 1, 10), (20, 2, 20), (30, 3, 30), (40, 4, 40)]


# ------------------------------------------------------------- latency / fills


def test_zero_latency_fills_at_next_bar_never_the_signal_bar() -> None:
    res = run_backtest(mk([100, 110, 120, 130]), once(buy()), cfg())
    (f,) = res.fills
    assert f.signal_ts_ns == 10 and f.timestamp_ns == 20
    assert f.price == D("110")  # close of the fill bar, not the signal bar (100)


def test_latency_delays_fill_to_first_event_after_delay() -> None:
    res = run_backtest(mk([100, 110, 120, 130, 140]), once(buy()), cfg(latency_ns=25))
    (f,) = res.fills
    assert f.timestamp_ns == 40 and f.price == D("130")  # eligible at 35 -> next bar at 40


def test_fill_price_open_option_uses_open_of_fill_bar() -> None:
    data = mk([100, 110, 120], opens=[100, 105, 115])
    res = run_backtest(data, once(buy()), cfg(fill_price=FillPrice.OPEN))
    assert res.fills[0].price == D("105")


def test_last_bar_signal_never_fills_and_is_reported_open() -> None:
    res = run_backtest(mk([100, 101]), once(buy(), at_call=1), cfg())
    assert res.fills == () and len(res.open_orders) == 1


# --------------------------------------------------------------------- costs


def test_spread_and_commission_are_applied_exactly() -> None:
    costs = CostModel(
        commission_per_share=D("0.01"), commission_rate=D("0.0005"), half_spread_bps=10.0
    )
    config = BacktestConfig(initial_cash=D("100000"), cost=costs)
    res = run_backtest(mk([100, 110, 120]), once(buy(10)), config)
    (f,) = res.fills
    assert f.price == D("110.11") and f.reference_price == D("110")
    assert f.fee == D("0.65055")  # 10*0.01 + 1101.1*0.0005
    assert res.final_portfolio.cash == D("100000") - D("1101.1") - D("0.65055")
    assert f.slippage_cost == D("1.1")  # (110.11 - 110) * 10


def test_sell_side_receives_worse_price() -> None:
    def strat(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        n = len(view.history("AAA"))
        return [buy(10)] if n == 1 else [OrderIntent.of("AAA", Side.SELL, 10)] if n == 2 else []

    config = BacktestConfig(cost=CostModel(half_spread_bps=10.0))
    res = run_backtest(mk([100, 110, 120, 130]), strat, config)
    assert [f.side for f in res.fills] == [Side.BUY, Side.SELL]
    assert res.fills[1].price == D("119.88")  # 120 * (1 - 0.001)
    (trade,) = res.trades
    assert trade.gross_pnl == D("119.88") * 10 - D("110.11") * 10


def test_minimum_commission_floor() -> None:
    costs = CostModel(commission_per_share=D("0.001"), min_commission=D("1.00"))
    res = run_backtest(
        mk([100, 100, 100]), once(buy(10)), BacktestConfig(cost=costs)
    )
    assert res.fills[0].fee == D("1.00")


def test_linear_impact_scales_with_participation() -> None:
    costs = CostModel(impact_model=ImpactModel.LINEAR, impact_coefficient=0.5)
    config = BacktestConfig(cost=costs)
    res = run_backtest(mk([100, 110, 120], volume=1000.0), once(buy(100)), config)
    # participation 100/1000 = 0.1 -> impact 0.5 * 0.1 = 5% on reference 110
    assert res.fills[0].price == D("115.5")


def test_sqrt_impact_uses_trailing_volatility_and_sqrt_scaling() -> None:
    closes = [100.0, 101.0, 99.0, 100.0, 102.0, 101.0]
    costs = CostModel(impact_model=ImpactModel.SQRT, impact_coefficient=1.0, vol_lookback=3)

    def slip(qty: float) -> float:
        res = run_backtest(
            mk(closes, volume=10_000.0), once(buy(qty), at_call=3), BacktestConfig(cost=costs)
        )
        f = res.fills[0]
        return float(f.price / f.reference_price - 1)

    logrets = np.diff(np.log(closes[:5]))[-3:]  # closes up to the fill bar (index 4)
    sigma = float(np.std(logrets, ddof=1))
    assert slip(100) == pytest.approx(sigma * math.sqrt(100 / 10_000), rel=1e-5)
    assert slip(400) / slip(100) == pytest.approx(2.0, rel=1e-4)  # sqrt law


# -------------------------------------------------------------- partial fills


def test_partial_fills_respect_volume_participation_cap() -> None:
    res = run_backtest(
        mk([100.0] * 6, volume=1000.0), once(buy(250)), cfg(participation_cap=0.1)
    )
    assert [(f.timestamp_ns, f.quantity) for f in res.fills] == [
        (20, D("100")),
        (30, D("100")),
        (40, D("50")),
    ]
    assert res.final_portfolio.positions["AAA"].quantity == D("250")
    assert res.open_orders == ()


def test_zero_volume_bar_does_not_fill() -> None:
    data = mk([100.0] * 3, volume=0.0)
    res = run_backtest(data, once(buy()), cfg())
    assert res.fills == ()


def test_competing_orders_share_the_bar_capacity() -> None:
    def strat(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        return [buy(80), buy(80)] if len(view.history("AAA")) == 1 else []

    res = run_backtest(mk([100.0] * 4, volume=1000.0), strat, cfg(participation_cap=0.1))
    at20 = [f.quantity for f in res.fills if f.timestamp_ns == 20]
    assert sum(at20) == D("100")  # 10% of 1000, shared FIFO: 80 + 20


# ------------------------------------------------------------ collar / limits


def test_price_collar_violation_rejects_the_order() -> None:
    res = run_backtest(mk([100, 103, 103]), once(buy()), cfg(collar_bps=200.0))
    assert res.fills == ()
    assert [r.reason for r in res.rejections] == ["price_collar"]


def test_price_within_collar_fills() -> None:
    res = run_backtest(mk([100, 103, 103]), once(buy()), cfg(collar_bps=400.0))
    assert len(res.fills) == 1 and res.rejections == ()


def test_limit_order_waits_until_price_is_acceptable() -> None:
    order = buy(5, price_limit=100)
    res = run_backtest(mk([100, 103, 99, 99]), once(order), cfg())
    (f,) = res.fills
    assert f.timestamp_ns == 30 and f.price == D("99")


def test_order_expires_after_ttl() -> None:
    order = buy(5, price_limit=100, valid_for_ns=15)
    res = run_backtest(mk([100, 103, 103, 99]), once(order), cfg())
    assert res.fills == ()
    assert [r.reason for r in res.rejections] == ["expired"]


def test_default_ttl_from_config() -> None:
    order = buy(5, price_limit=100)
    res = run_backtest(mk([100, 103, 103, 99]), once(order), cfg(default_ttl_ns=15))
    assert res.fills == () and res.rejections[0].reason == "expired"


# --------------------------------------------------------------- order rules


def test_sell_without_long_position_is_rejected() -> None:
    res = run_backtest(mk([100, 101, 102]), once(OrderIntent.of("AAA", Side.SELL, 5)), cfg())
    assert res.fills == () and res.rejections[0].reason == "no_long_position"


def test_short_selling_disabled_by_default_and_enabled_by_config() -> None:
    short = OrderIntent.of("AAA", Side.SELL_SHORT, 5)
    off = run_backtest(mk([100, 101, 102]), once(short), cfg())
    assert off.rejections[0].reason == "shorting_disabled"
    on = run_backtest(mk([100, 101, 102]), once(short), cfg(allow_short=True))
    assert on.final_portfolio.positions["AAA"].quantity == D("-5")


def test_insufficient_cash_rejects_buy() -> None:
    config = BacktestConfig(initial_cash=D("1000"))
    res = run_backtest(mk([100, 100, 100]), once(buy(100)), config)
    assert res.fills == () and res.rejections[0].reason == "insufficient_cash"


def test_omega_below_threshold_abstains() -> None:
    lo, hi = buy(5, omega=0.5), buy(5, omega=0.6)
    config = cfg(omega_threshold=0.55)
    assert run_backtest(mk([100, 101, 102]), once(lo), config).fills == ()
    assert len(run_backtest(mk([100, 101, 102]), once(lo), config).abstained) == 1
    assert len(run_backtest(mk([100, 101, 102]), once(hi), config).fills) == 1
    assert len(run_backtest(mk([100, 101, 102]), once(buy(5)), config).fills) == 1  # no omega


def test_bad_strategy_output_and_unknown_symbol_raise() -> None:
    bad: Strategy = lambda v, p: ["nope"]  # type: ignore[list-item]  # noqa: E731
    with pytest.raises(TypeError):
        run_backtest(mk([100, 101]), bad, cfg())
    ghost = once(OrderIntent.of("ZZZ", Side.BUY, 1))
    with pytest.raises(DataValidationError):
        run_backtest(mk([100, 101]), ghost, cfg())


# ------------------------------------------------------- accounting / ledger


def _alternating(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
    h = view.history("AAA")
    n = len(h)
    if n < 3:
        return []
    up = h.closes(2)[1] > h.closes(2)[0]
    held = port.quantity("AAA")
    if up and held == 0:
        return [OrderIntent.of("AAA", Side.BUY, 50)]
    if not up and held > 0:
        return [OrderIntent.of("AAA", Side.SELL, held)]
    return []


def test_cash_conservation_and_ledger_reconcile_with_equity() -> None:
    data = generate_market_data(["AAA"], 300, seed=11)
    config = BacktestConfig(
        cost=CostModel(commission_per_share=D("0.005"), half_spread_bps=2.0),
        execution=ExecutionConfig(participation_cap=0.5),
    )
    res = run_backtest(data, _alternating, config)
    assert len(res.fills) > 10
    marks = {"AAA": D(str(data.series("AAA").close[-1]))}
    res.final_portfolio.check_invariants(marks)  # raises on any violation
    # Every round trip is accounted for: cash P&L == sum of net trade P&L when flat.
    if "AAA" not in res.final_portfolio.positions:
        pnl = res.final_portfolio.cash - config.initial_cash
        assert pnl == sum((t.net_pnl for t in res.trades), start=D(0))
    assert res.metrics.n_trades == len(res.trades)
    assert res.equity.equity.shape == res.equity.ts_ns.shape


# --------------------------------------------------------------- determinism


def test_same_seed_same_result_different_seed_differs_with_jitter() -> None:
    data = generate_market_data(["AAA"], 200, seed=5)
    jitter = 3 * 86_400 * 10**9
    base = BacktestConfig(execution=ExecutionConfig(latency_ns=1, latency_jitter_ns=jitter))
    a = run_backtest(data, _alternating, base)
    b = run_backtest(data, _alternating, base)
    assert a.fingerprint() == b.fingerprint()
    np.testing.assert_array_equal(a.equity.equity, b.equity.equity)
    other = BacktestConfig(
        execution=base.execution, seed=base.seed + 1
    )
    assert run_backtest(data, _alternating, other).fingerprint() != a.fingerprint()


def test_no_randomness_without_jitter_means_seed_is_irrelevant() -> None:
    data = generate_market_data(["AAA"], 100, seed=5)
    a = run_backtest(data, _alternating, BacktestConfig(seed=1))
    b = run_backtest(data, _alternating, BacktestConfig(seed=2))
    assert a.fingerprint() == b.fingerprint()


# --------------------------------------------------- start time / regime / cfg


def test_trading_start_gives_strategy_warmup_history_but_no_earlier_orders() -> None:
    lengths: list[int] = []

    def rec(view: MarketView, port: PortfolioSnapshot) -> Sequence[OrderIntent]:
        lengths.append(len(view.history("AAA")))
        return []

    data = mk([100.0 + i for i in range(10)])
    res = run_backtest(data, rec, BacktestConfig(trading_start_ts_ns=50))
    assert lengths == [5, 6, 7, 8, 9, 10]
    assert int(res.equity.ts_ns[0]) == 50 and res.equity.ts_ns.shape[0] == 6


def test_equity_curve_carries_regime_of_each_event() -> None:
    data = generate_market_data(
        ["AAA"], 6, seed=2, regimes=[RegimeLabel.CRISIS] * 3 + [RegimeLabel.LOW_VOL_CHOP] * 3
    )
    res = run_backtest(data, lambda v, p: [], BacktestConfig())
    assert list(res.equity.regime) == [5, 5, 5, 4, 4, 4]


def test_config_validation() -> None:
    with pytest.raises(ConfigError):
        ExecutionConfig(participation_cap=0.0)
    with pytest.raises(ConfigError):
        ExecutionConfig(latency_ns=-1)
    with pytest.raises(ConfigError):
        BacktestConfig(initial_cash=D("0"))
    with pytest.raises(ConfigError):
        CostModel(half_spread_bps=-1.0)
    with pytest.raises(ConfigError):
        ExecutionConfig(omega_threshold=2.0)
