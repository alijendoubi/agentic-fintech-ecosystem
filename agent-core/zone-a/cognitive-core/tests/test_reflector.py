import pytest

from models import RegimeLabel, RubricChangeProposal, SignalSide, SignalStatus, TradeSignal
from reflector import build_rubric_change_proposal


class _StubClient:
    def __init__(self, payload: str):
        self._payload = payload

    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
        return self._payload


def _closed_signal() -> TradeSignal:
    return TradeSignal(
        signal_id="sig-42",
        symbol="AAPL",
        side=SignalSide.BUY,
        quantity=10,
        omega=0.7,
        expected_value=50.0,
        p_success=0.6,
        p_failure=0.4,
        reward_estimate=100.0,
        risk_estimate=50.0,
        regime=RegimeLabel.TRENDING_BULL,
        regime_confidence=0.8,
        debate_summary="bought on momentum, judge sided BUY",
        status=SignalStatus.SIGNAL_APPROVED,
    )


@pytest.mark.asyncio
async def test_reflector_requires_human_gate():
    proposal = await build_rubric_change_proposal(
        trade_signal=_closed_signal(),
        outcome="loss: -2.1%, regime flipped to HIGH_VOL_CHOP mid-trade",
        observed_underperformance="3 consecutive BUY signals in TRENDING_BULL lost money after a regime flip",
        client=_StubClient("Add a regime-flip check between Judge and Compression"),
    )

    assert isinstance(proposal, RubricChangeProposal)
    assert proposal.trigger_signal_id == "sig-42"
    # No field on this model can cause a live effect — promotion is a
    # separate, human-gated workflow (docs/processes/sharp-promotion.md).
    assert not hasattr(proposal, "applied")
    assert not hasattr(proposal, "deployed")
    assert "regime-flip check" in proposal.proposed_change


@pytest.mark.asyncio
async def test_reflector_timeout_does_not_raise():
    class _HangingClient:
        async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
            import asyncio

            await asyncio.sleep(10)
            return "never gets here"

    proposal = await build_rubric_change_proposal(
        trade_signal=_closed_signal(),
        outcome="loss",
        observed_underperformance="pattern",
        client=_HangingClient(),
        timeout_s=0.05,
    )
    assert "timed out" in proposal.proposed_change
