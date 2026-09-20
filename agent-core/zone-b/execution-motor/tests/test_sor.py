from __future__ import annotations

from decimal import Decimal

import pytest
from execution_motor.models import Side
from execution_motor.sor import RouterConfig, SmartOrderRouter, UnscoredPolicy
from execution_motor.toxicity import ToxicityParams, VenueObservation

from .helpers import NOW_NS, make_order

D = Decimal
PARAMS = ToxicityParams(min_observations=2)


def _obs(fill: str, after: str, filled: str = "100", ts: int = NOW_NS - 1000) -> VenueObservation:
    return VenueObservation(
        timestamp_ns=ts,
        side=Side.BUY,
        ordered_qty=D("100"),
        filled_qty=D(filled),
        avg_fill_price=D(fill),
        arrival_mid=D("100"),
        mid_after_horizon=D(after),
    )


def _stats(score_kind: str) -> list[VenueObservation]:
    """benign: score 0.  mild: ~0.16.  toxic: ~0.9 (components saturated, half filled)."""
    if score_kind == "benign":
        return [_obs("100", "100.10")] * 2
    if score_kind == "mild":
        return [_obs("100.02", "100.01")] * 2  # slip 2bps, markout -1bp -> score ~0.16
    return [_obs("101", "99", filled="50")] * 2


def _router(**cfg: object) -> SmartOrderRouter:
    base: dict[str, object] = {"max_toxicity": D("0.5"), "params": PARAMS, "window_days": 5}
    base.update(cfg)
    return SmartOrderRouter(RouterConfig(**base))  # type: ignore[arg-type]


def test_routes_to_lowest_toxicity_venue() -> None:
    decision = _router().route(
        make_order(),
        ["A", "B"],
        {"A": _stats("benign"), "B": _stats("mild")},
        now_ns=NOW_NS,
    )
    assert decision.venue == "A"


def test_avoids_venue_above_threshold() -> None:
    decision = _router().route(
        make_order(), ["A", "B"], {"A": _stats("toxic"), "B": _stats("benign")}, now_ns=NOW_NS
    )
    assert decision.venue == "B"
    assessed = {a.venue: a for a in decision.assessments}
    assert assessed["A"].eligible is False
    assert assessed["A"].score is not None and assessed["A"].score > D("0.5")


def test_all_venues_toxic_means_no_route() -> None:
    decision = _router().route(make_order(), ["A"], {"A": _stats("toxic")}, now_ns=NOW_NS)
    assert decision.venue is None


def test_score_exactly_at_threshold_is_allowed() -> None:
    decision = _router(max_toxicity=D("0")).route(
        make_order(), ["A"], {"A": _stats("benign")}, now_ns=NOW_NS
    )
    assert decision.venue == "A"


def test_unscored_venue_denied_by_default() -> None:
    decision = _router().route(make_order(), ["A"], {}, now_ns=NOW_NS)
    assert decision.venue is None
    assert decision.assessments[0].score is None


def test_insufficient_evidence_is_unscored() -> None:
    decision = _router().route(make_order(), ["A"], {"A": _stats("benign")[:1]}, now_ns=NOW_NS)
    assert decision.venue is None


def test_unscored_allowed_by_policy_ranks_after_scored() -> None:
    router = _router(unscored_policy=UnscoredPolicy.ALLOW)
    only_unscored = router.route(make_order(), ["A"], {}, now_ns=NOW_NS)
    assert only_unscored.venue == "A"
    both = router.route(make_order(), ["A", "B"], {"B": _stats("benign")}, now_ns=NOW_NS)
    assert both.venue == "B"


def test_order_level_threshold_can_tighten_but_not_loosen() -> None:
    tighter = make_order(max_venue_toxicity=D("0.01"))
    assert _router().route(tighter, ["A"], {"A": _stats("mild")}, now_ns=NOW_NS).venue is None
    looser = make_order(max_venue_toxicity=D("0.99"))
    assert _router().route(looser, ["A"], {"A": _stats("toxic")}, now_ns=NOW_NS).venue is None


def test_preferred_venue_honoured_only_when_eligible() -> None:
    stats = {"A": _stats("benign"), "B": _stats("mild"), "C": _stats("toxic")}
    router = _router()
    assert (
        router.route(make_order(preferred_venue="B"), ["A", "B", "C"], stats, now_ns=NOW_NS).venue
        == "B"
    )
    assert (
        router.route(make_order(preferred_venue="C"), ["A", "B", "C"], stats, now_ns=NOW_NS).venue
        == "A"
    )
    assert (
        router.route(make_order(preferred_venue="ZZZ"), ["A", "B", "C"], stats, now_ns=NOW_NS).venue
        == "A"
    )


def test_stale_observations_outside_window_are_ignored() -> None:
    old = [_obs("100", "100.10", ts=NOW_NS - 6 * 86_400_000_000_000)] * 2
    decision = _router().route(make_order(), ["A"], {"A": old}, now_ns=NOW_NS)
    assert decision.venue is None  # nothing left in-window -> unscored -> denied


def test_ties_break_deterministically_by_name() -> None:
    stats = {"B": _stats("benign"), "A": _stats("benign")}
    assert _router().route(make_order(), ["B", "A"], stats, now_ns=NOW_NS).venue == "A"


def test_no_candidates_no_route() -> None:
    assert _router().route(make_order(), [], {}, now_ns=NOW_NS).venue is None


def test_router_config_validation() -> None:
    with pytest.raises(ValueError):
        RouterConfig(max_toxicity=D("1.5"))
    with pytest.raises(ValueError):
        RouterConfig(window_days=0)
