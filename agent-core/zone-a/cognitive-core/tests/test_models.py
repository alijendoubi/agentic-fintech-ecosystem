import pytest
from pydantic import ValidationError

from models import (
    JudgeVerdict,
    MAX_SUMMARY_TOKENS,
    RegimeLabel,
    SignalSide,
    SignalStatus,
    TradeSignal,
)


def _judge_kwargs(**overrides):
    base = dict(
        side=SignalSide.BUY,
        omega=0.7,
        p_success=0.6,
        p_failure=0.4,
        reward_estimate=100.0,
        risk_estimate=50.0,
    )
    base.update(overrides)
    return base


def test_omega_bounds():
    JudgeVerdict(**_judge_kwargs(omega=0.0))
    JudgeVerdict(**_judge_kwargs(omega=1.0))
    with pytest.raises(ValidationError):
        JudgeVerdict(**_judge_kwargs(omega=1.5))
    with pytest.raises(ValidationError):
        JudgeVerdict(**_judge_kwargs(omega=-0.1))


def test_signal_side_enum_matches_proto():
    # trade_signal.proto SignalSide: SIDE_UNKNOWN=0, BUY=1, SELL=2, SELL_SHORT=3
    assert {s.value for s in SignalSide} == {"SIDE_UNKNOWN", "BUY", "SELL", "SELL_SHORT"}


def test_signal_status_enum_matches_proto():
    assert {s.value for s in SignalStatus} == {
        "SIGNAL_PENDING",
        "SIGNAL_APPROVED",
        "SIGNAL_REJECTED_HARD_BLOCK",
        "SIGNAL_SOFT_BLOCK_PENDING",
        "SIGNAL_ABSTAIN",
        "SIGNAL_EXPIRED",
    }


def test_debate_summary_token_cap():
    oversized = "x" * (MAX_SUMMARY_TOKENS * 4 + 500)
    signal = TradeSignal(
        signal_id="sig-1",
        symbol="AAPL",
        side=SignalSide.BUY,
        quantity=10,
        omega=0.8,
        expected_value=1.0,
        p_success=0.6,
        p_failure=0.4,
        reward_estimate=1.0,
        risk_estimate=0.5,
        regime=RegimeLabel.TRENDING_BULL,
        regime_confidence=0.9,
        debate_summary=oversized,
    )
    assert len(signal.debate_summary) == MAX_SUMMARY_TOKENS * 4
