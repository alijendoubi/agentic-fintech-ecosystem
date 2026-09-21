from __future__ import annotations

from decimal import Decimal

import pytest

from execution_motor.models import Side
from execution_motor.toxicity import (
    ToxicityParams,
    VenueObservation,
    compute_toxicity,
    filter_window,
    markout_bps,
    slippage_bps,
)

D = Decimal
DAY_NS = 86_400_000_000_000


def obs(
    side: Side = Side.BUY,
    ordered: str = "100",
    filled: str = "100",
    fill: str | None = "100.05",
    arrival: str = "100",
    after: str | None = "99.999975",
    ts: int = 1,
) -> VenueObservation:
    return VenueObservation(
        timestamp_ns=ts,
        side=side,
        ordered_qty=D(ordered),
        filled_qty=D(filled),
        avg_fill_price=D(fill) if fill is not None else None,
        arrival_mid=D(arrival),
        mid_after_horizon=D(after) if after is not None else None,
    )


PARAMS = ToxicityParams(
    markout_scale_bps=D(5),
    slippage_scale_bps=D(10),
    weight_markout=D("0.5"),
    weight_slippage=D("0.3"),
    weight_fill_shortfall=D("0.2"),
    min_observations=2,
)


def test_markout_is_negative_when_price_moves_against_us() -> None:
    assert markout_bps(Side.BUY, D("100"), D("99.95")) == D("-5")
    assert markout_bps(Side.SELL, D("100"), D("100.05")) == D("-5")


def test_markout_is_positive_when_price_moves_our_way() -> None:
    assert markout_bps(Side.BUY, D("100"), D("100.10")) == D("10")
    assert markout_bps(Side.SELL, D("100"), D("99.90")) == D("10")


def test_slippage_positive_means_we_paid_up() -> None:
    assert slippage_bps(Side.BUY, D("100.02"), D("100")) == D("2")
    assert slippage_bps(Side.SELL, D("99.98"), D("100")) == D("2")
    assert slippage_bps(Side.BUY, D("99.98"), D("100")) == D("-2")


def test_hand_computed_score() -> None:
    # obs1: buy 100/100, slip +5bps, markout -5bps.  obs2: sell 50/100, slip 0, markout 0.
    o1 = obs()
    o2 = obs(side=Side.SELL, filled="50", fill="100", after="100")
    result = compute_toxicity([o1, o2], PARAMS)
    assert result is not None
    # size-weighted markout = (100*-5 + 50*0)/150 = -3.3333 -> adverse component 0.66667
    # size-weighted slippage = (100*5)/150 = 3.3333          -> component 0.33333
    # fill rate = 150/200 = .75                              -> shortfall 0.25
    # score = .5*.66667 + .3*.33333 + .2*.25 = .483333
    assert abs(result.mean_markout_bps - D("-3.333333")) < D("0.00001")
    assert abs(result.mean_slippage_bps - D("3.333333")) < D("0.00001")
    assert result.fill_rate == D("0.75")
    assert abs(result.score - D("0.483333")) < D("0.00001")
    assert result.n_fills == 2


def test_favourable_markout_does_not_reduce_score_below_other_components() -> None:
    good = obs(fill="100", after="100.50", filled="100")
    result = compute_toxicity([good, good], PARAMS)
    assert result is not None
    assert result.score == D("0")


def test_components_saturate_at_one() -> None:
    awful = obs(fill="101", after="99", filled="10")
    result = compute_toxicity([awful, awful], PARAMS)
    assert result is not None
    assert result.score == D("0.5") + D("0.3") + D("0.2") * D("0.9")


def test_insufficient_filled_observations_yields_none() -> None:
    assert compute_toxicity([obs()], PARAMS) is None
    unfilled = obs(filled="0", fill=None, after=None)
    assert compute_toxicity([obs(), unfilled], PARAMS) is None
    assert compute_toxicity([], PARAMS) is None


def test_unfilled_observations_still_count_for_fill_rate() -> None:
    unfilled = obs(filled="0", fill=None, after=None)
    result = compute_toxicity([obs(), obs(), unfilled], PARAMS)
    assert result is not None
    assert result.fill_rate == D("200") / D("300")


def test_score_is_always_within_unit_interval() -> None:
    for fill, after, filled in [
        ("90", "110", "100"),
        ("110", "90", "1"),
        ("100", "100", "100"),
        ("100.01", "99.99", "37"),
    ]:
        for side in (Side.BUY, Side.SELL):
            o = obs(side=side, fill=fill, after=after, filled=filled)
            r = compute_toxicity([o, o], PARAMS)
            assert r is not None
            assert D("0") <= r.score <= D("1")


def test_filled_observation_requires_prices() -> None:
    with pytest.raises(ValueError):
        obs(fill=None)
    with pytest.raises(ValueError):
        obs(after=None)


def test_observation_validation() -> None:
    with pytest.raises(ValueError):
        obs(ordered="0")
    with pytest.raises(ValueError):
        obs(filled="101")
    with pytest.raises(ValueError):
        obs(arrival="0")


def test_params_validation() -> None:
    with pytest.raises(ValueError):
        ToxicityParams(
            weight_markout=D("0.5"), weight_slippage=D("0.5"), weight_fill_shortfall=D("0.5")
        )
    with pytest.raises(ValueError):
        ToxicityParams(markout_scale_bps=D("0"))
    with pytest.raises(ValueError):
        ToxicityParams(min_observations=0)


def test_default_params_weights_sum_to_one() -> None:
    p = ToxicityParams()
    assert p.weight_markout + p.weight_slippage + p.weight_fill_shortfall == D("1")


def test_window_filter_keeps_only_recent_observations() -> None:
    now = 10 * DAY_NS
    old = obs(ts=now - 6 * DAY_NS)
    edge = obs(ts=now - 5 * DAY_NS)
    fresh = obs(ts=now - DAY_NS)
    future = obs(ts=now + DAY_NS)
    kept = filter_window([old, edge, fresh, future], now_ns=now, window_days=5)
    assert kept == (edge, fresh)
