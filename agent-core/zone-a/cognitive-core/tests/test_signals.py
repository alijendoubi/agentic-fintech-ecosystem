from __future__ import annotations

import math
from typing import Any

import pytest

from cognitive_core.models import (
    BlueThesis,
    DebateState,
    JudgeVerdict,
    RedChallenge,
    SignalSide,
    SignalStatus,
)
from cognitive_core.signals import NO_SUMMARY, abstain_signal, abstain_reasons, to_trade_signal
from cognitive_core.tests.fakes import initial_state, make_settings

SETTINGS = make_settings()
NOW = 1_000_000_000


def _verdict(**overrides: Any) -> JudgeVerdict:
    base: dict[str, Any] = {
        "side": SignalSide.BUY, "omega": 0.72, "p_success": 0.65, "p_failure": 0.35,
        "reward_estimate": 120.0, "risk_estimate": 40.0,
    }
    return JudgeVerdict(**{**base, **overrides})


def _state(verdict: JudgeVerdict | None = None, **overrides: Any) -> DebateState:
    update: dict[str, Any] = {
        "blue_thesis": BlueThesis(side=SignalSide.BUY, rationale="r"),
        "red_challenge": RedChallenge(counter_factors=("c",)),
        "judge_verdict": verdict if verdict is not None else _verdict(),
        "debate_summary": "summary",
        **overrides,
    }
    return initial_state().model_copy(update=update)


def _signal(state: DebateState, quantity: float = 10.0) -> Any:
    return to_trade_signal(state, settings=SETTINGS, quantity=quantity, now_ns=NOW)


def test_pending_path_populates_all_fields() -> None:
    sig = _signal(_state())
    assert sig.status == SignalStatus.SIGNAL_PENDING
    assert sig.side == SignalSide.BUY and sig.quantity == 10.0
    assert sig.created_at_ns == NOW
    assert sig.valid_until_ns == NOW + SETTINGS.signal_ttl_ms * 1_000_000
    assert sig.expected_value == pytest.approx(0.65 * 120 - 0.35 * 40)
    assert sig.symbol == "AAPL" and sig.debate_summary == "summary"


def test_omega_exactly_at_threshold_is_actionable() -> None:
    sig = _signal(_state(_verdict(omega=SETTINGS.omega_threshold)))
    assert sig.status == SignalStatus.SIGNAL_PENDING


def test_omega_just_below_threshold_abstains() -> None:
    sig = _signal(_state(_verdict(omega=SETTINGS.omega_threshold - 1e-9)))
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN
    assert sig.side == SignalSide.SIDE_UNKNOWN and sig.quantity == 0.0


def test_unknown_side_with_high_omega_abstains() -> None:
    sig = _signal(_state(_verdict(side=SignalSide.SIDE_UNKNOWN, omega=0.99)))
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN


@pytest.mark.parametrize("qty", [0.0, -5.0, math.nan, math.inf * -1])
def test_non_positive_quantity_abstains(qty: float) -> None:
    sig = _signal(_state(), quantity=qty)
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN and sig.quantity == 0.0


def test_default_quantity_never_trades() -> None:
    sig = to_trade_signal(_state(), settings=SETTINGS, now_ns=NOW)
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN


def test_forced_blue_and_red_then_judge_buy_abstains() -> None:
    state = _state(
        blue_thesis=BlueThesis(side=SignalSide.SIDE_UNKNOWN, rationale="f", forced_completion=True),
        red_challenge=RedChallenge(forced_completion=True),
        judge_verdict=_verdict(omega=0.95),
    )
    sig = _signal(state)
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN


@pytest.mark.parametrize("who", ["blue", "red"])
def test_any_single_forced_upstream_node_abstains(who: str) -> None:
    key = "blue_thesis" if who == "blue" else "red_challenge"
    forced = (
        BlueThesis(side=SignalSide.BUY, rationale="r", forced_completion=True)
        if who == "blue"
        else RedChallenge(forced_completion=True)
    )
    assert _signal(_state(**{key: forced})).status == SignalStatus.SIGNAL_ABSTAIN


def test_defaulted_abstain_verdict_abstains() -> None:
    assert _signal(_state(JudgeVerdict.default_abstain())).status == SignalStatus.SIGNAL_ABSTAIN


def test_non_positive_expected_value_abstains() -> None:
    v = _verdict(p_success=0.2, p_failure=0.8, reward_estimate=10.0, risk_estimate=40.0)
    sig = _signal(_state(v))
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN and sig.expected_value < 0


def test_reward_minus_risk_positive_but_ev_negative_abstains() -> None:
    # The old formula (reward - risk = 60) would look profitable; the true E[V] is negative.
    v = _verdict(p_success=0.1, p_failure=0.9, reward_estimate=100.0, risk_estimate=40.0)
    assert v.reward_estimate - v.risk_estimate > 0 > v.expected_value
    assert _signal(_state(v)).status == SignalStatus.SIGNAL_ABSTAIN


def test_missing_judge_verdict_abstains_without_raising() -> None:
    state = initial_state().model_copy(update={"debate_summary": "s"})
    sig = to_trade_signal(state, settings=SETTINGS, quantity=10.0, now_ns=NOW)
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN


def test_missing_summary_uses_placeholder_and_missing_upstream_abstains() -> None:
    state = _state(blue_thesis=None, debate_summary=None)
    sig = _signal(state)
    assert sig.debate_summary == NO_SUMMARY and sig.status == SignalStatus.SIGNAL_ABSTAIN
    assert "missing_upstream_node" in abstain_reasons(state, _verdict(), SETTINGS, 10.0)


def test_summary_truncated_to_configured_cap() -> None:
    settings = make_settings(COGNITIVE_COMPRESSION_MAX_TOKENS="5")
    sig = to_trade_signal(
        _state(debate_summary="z" * 100), settings=settings, quantity=1.0, now_ns=NOW
    )
    assert len(sig.debate_summary) == 20


def test_higher_threshold_from_settings_is_respected() -> None:
    strict = make_settings(COGNITIVE_OMEGA_THRESHOLD="0.9")
    sig = to_trade_signal(_state(), settings=strict, quantity=10.0, now_ns=NOW)
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN


def test_signal_ids_are_unique() -> None:
    assert _signal(_state()).signal_id != _signal(_state()).signal_id


def test_abstain_signal_is_flat_and_carries_summary() -> None:
    sig = abstain_signal(initial_state(), settings=SETTINGS, summary="aborted", now_ns=NOW)
    assert sig.status == SignalStatus.SIGNAL_ABSTAIN and sig.debate_summary == "aborted"
    assert sig.side == SignalSide.SIDE_UNKNOWN and sig.omega == 0.0


def test_default_now_uses_wall_clock() -> None:
    sig = to_trade_signal(_state(), settings=SETTINGS, quantity=1.0)
    assert sig.created_at_ns > 1_600_000_000 * 10**9
