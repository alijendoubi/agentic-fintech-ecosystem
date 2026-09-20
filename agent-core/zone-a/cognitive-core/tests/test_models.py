import math
from typing import Any

import pytest
from pydantic import ValidationError

from cognitive_core.models import (
    MAX_SUMMARY_TOKENS,
    REQUIRED_STAGES,
    BlueThesis,
    JudgeVerdict,
    MarketContext,
    ProposalStage,
    RedChallenge,
    RegimeLabel,
    RubricChangeProposal,
    SignalSide,
    SignalStatus,
    TradeSignal,
)


def _judge(**overrides: Any) -> JudgeVerdict:
    base: dict[str, Any] = {
        "side": SignalSide.BUY,
        "omega": 0.7,
        "p_success": 0.6,
        "p_failure": 0.4,
        "reward_estimate": 100.0,
        "risk_estimate": 50.0,
    }
    return JudgeVerdict(**{**base, **overrides})


def _signal(**overrides: Any) -> TradeSignal:
    base: dict[str, Any] = {
        "signal_id": "sig-1",
        "symbol": "AAPL",
        "created_at_ns": 1_000,
        "side": SignalSide.BUY,
        "quantity": 10.0,
        "omega": 0.8,
        "expected_value": 1.0,
        "p_success": 0.6,
        "p_failure": 0.4,
        "reward_estimate": 1.0,
        "risk_estimate": 0.5,
        "regime": RegimeLabel.TRENDING_BULL,
        "regime_confidence": 0.9,
        "debate_summary": "s",
        "valid_until_ns": 2_000,
    }
    return TradeSignal(**{**base, **overrides})


def test_omega_bounds() -> None:
    _judge(omega=0.0)
    _judge(omega=1.0)
    for bad in (1.5, -0.1):
        with pytest.raises(ValidationError):
            _judge(omega=bad)


@pytest.mark.parametrize("field", ["omega", "p_success", "reward_estimate", "risk_estimate"])
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_judge_rejects_non_finite(field: str, bad: float) -> None:
    with pytest.raises(ValidationError):
        _judge(**{field: bad})


def test_judge_rejects_probabilities_out_of_range() -> None:
    with pytest.raises(ValidationError):
        _judge(p_success=1.2, p_failure=-0.2)


def test_judge_rejects_probabilities_not_summing_to_one() -> None:
    with pytest.raises(ValidationError, match="must equal 1"):
        _judge(p_success=0.6, p_failure=0.5)


def test_judge_accepts_sum_within_tolerance() -> None:
    _judge(p_success=0.6, p_failure=0.4 + 5e-7)


@pytest.mark.parametrize("field", ["reward_estimate", "risk_estimate"])
def test_judge_rejects_negative_reward_or_risk(field: str) -> None:
    with pytest.raises(ValidationError):
        _judge(**{field: -1.0})


def test_expected_value_uses_probabilities() -> None:
    v = _judge(p_success=0.6, p_failure=0.4, reward_estimate=100.0, risk_estimate=50.0)
    assert v.expected_value == pytest.approx(0.6 * 100 - 0.4 * 50)
    assert v.expected_value != v.reward_estimate - v.risk_estimate


def test_default_abstain_is_valid_and_flagged() -> None:
    v = JudgeVerdict.default_abstain()
    assert v.defaulted_abstain and v.omega == 0.0 and v.side == SignalSide.SIDE_UNKNOWN


def test_judge_verdict_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _judge().omega = 0.1  # type: ignore[misc]


def test_llm_models_reject_unknown_fields_and_wrong_types() -> None:
    with pytest.raises(ValidationError):
        BlueThesis.model_validate_json('{"side": "BUY", "rationale": "r", "extra": 1}')
    with pytest.raises(ValidationError):
        JudgeVerdict.model_validate_json(
            '{"side":"BUY","omega":true,"p_success":0.5,"p_failure":0.5,'
            '"reward_estimate":1,"risk_estimate":1}'
        )
    with pytest.raises(ValidationError):
        RedChallenge.model_validate_json('{"counter_factors": "not-a-list"}')


def test_llm_models_parse_json_lists_into_tuples() -> None:
    thesis = BlueThesis.model_validate_json(
        '{"side": "SELL", "rationale": "r", "key_factors": ["a", "b"]}'
    )
    assert thesis.key_factors == ("a", "b") and thesis.side is SignalSide.SELL


def test_market_context_validation() -> None:
    kwargs: dict[str, Any] = {
        "symbol": "AAPL", "mid_price": 1.0, "z_score": 0.0, "mad_score": 0.0,
        "ofi": 0.0, "realized_vol": 0.1, "adv_30d": 1.0,
    }
    MarketContext(**kwargs)
    for override in ({"ofi": 1.5}, {"mid_price": 0.0}, {"symbol": ""}, {"z_score": math.nan}):
        with pytest.raises(ValidationError):
            MarketContext(**{**kwargs, **override})


def test_regime_unknown_exists_and_enums_are_strenum() -> None:
    assert RegimeLabel.REGIME_UNKNOWN == "REGIME_UNKNOWN"
    assert isinstance(SignalSide.BUY, str) and f"{SignalSide.BUY}" == "BUY"
    assert isinstance(SignalStatus.SIGNAL_ABSTAIN, str)


def test_debate_summary_token_cap() -> None:
    sig = _signal(debate_summary="x" * (MAX_SUMMARY_TOKENS * 4 + 500))
    assert len(sig.debate_summary) == MAX_SUMMARY_TOKENS * 4


def test_signal_pending_with_unknown_side_is_rejected() -> None:
    with pytest.raises(ValidationError, match="SIDE_UNKNOWN"):
        _signal(side=SignalSide.SIDE_UNKNOWN)


@pytest.mark.parametrize("qty", [0.0, math.nan])
def test_signal_pending_with_non_positive_quantity_is_rejected(qty: float) -> None:
    with pytest.raises(ValidationError):
        _signal(quantity=qty)


def test_signal_pending_needs_positive_validity_window() -> None:
    with pytest.raises(ValidationError, match="valid_until_ns"):
        _signal(valid_until_ns=1_000)


def test_signal_abstain_must_be_flat() -> None:
    _signal(status=SignalStatus.SIGNAL_ABSTAIN, side=SignalSide.SIDE_UNKNOWN, quantity=0.0)
    with pytest.raises(ValidationError, match="abstain"):
        _signal(status=SignalStatus.SIGNAL_ABSTAIN)


def test_signal_probabilities_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        _signal(p_success=0.9, p_failure=0.9)


def test_signal_cost_fields_default_zero_and_reject_negative() -> None:
    sig = _signal()
    assert sig.estimated_total_cost == 0.0
    with pytest.raises(ValidationError):
        _signal(estimated_venue_fees=-1.0)


def _proposal(**overrides: Any) -> RubricChangeProposal:
    base: dict[str, Any] = {
        "proposal_id": "p1", "trigger_signal_id": "s1", "created_at_ns": 1,
        "observed_underperformance": "x", "proposed_change": "y", "rationale": "z",
        "realised_pnl": -2.0, "regime_at_close": RegimeLabel.CRISIS, "debate_history": "h",
    }
    return RubricChangeProposal(**{**base, **overrides})


def test_proposal_is_draft_with_full_gate() -> None:
    p = _proposal()
    assert p.status == "DRAFT" and p.requires_human_signoff is True
    assert p.required_stages == REQUIRED_STAGES
    assert [s.value for s in p.required_stages] == [
        "COMPLIANCE", "LEGAL", "BACKTEST", "RISK", "CANARY",
    ]


@pytest.mark.parametrize(
    "override",
    [
        {"status": "APPLIED"},
        {"requires_human_signoff": False},
        {"required_stages": (ProposalStage.CANARY,)},
        {"rationale": ""},
    ],
)
def test_proposal_cannot_be_weakened(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _proposal(**override)


def test_proposal_has_no_apply_surface() -> None:
    fields = set(RubricChangeProposal.model_fields)
    assert not fields & {"applied", "deployed", "apply", "deploy"}
    assert not any(hasattr(RubricChangeProposal, n) for n in ("apply", "deploy", "promote"))
