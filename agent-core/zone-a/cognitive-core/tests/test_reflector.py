from __future__ import annotations

import asyncio

import pytest

from cognitive_core.models import (
    REQUIRED_STAGES,
    RegimeLabel,
    RubricChangeProposal,
    SignalSide,
    SignalStatus,
    TradeOutcome,
    TradeSignal,
)
from cognitive_core.reflector import build_rubric_change_proposal
from cognitive_core.tests.fakes import HangingClient, ScriptedClient, make_settings

DRAFT_JSON = (
    '{"proposed_change": "Add a regime-flip check between Judge and Compression", '
    '"rationale": "Loss followed a flip to HIGH_VOL_CHOP that Judge did not price in"}'
)
OUTCOME = TradeOutcome(
    realised_pnl=-2.1, regime_at_close=RegimeLabel.HIGH_VOL_CHOP, description="flip mid-trade"
)


def _closed_signal() -> TradeSignal:
    return TradeSignal(
        signal_id="sig-42", symbol="AAPL", created_at_ns=1, side=SignalSide.BUY, quantity=10.0,
        omega=0.7, expected_value=50.0, p_success=0.6, p_failure=0.4, reward_estimate=100.0,
        risk_estimate=50.0, regime=RegimeLabel.TRENDING_BULL, regime_confidence=0.8,
        debate_summary="bought on momentum, judge sided BUY", valid_until_ns=2,
        status=SignalStatus.SIGNAL_APPROVED,
    )


async def _build(client: object, **kwargs: object) -> RubricChangeProposal | None:
    return await build_rubric_change_proposal(
        _closed_signal(), OUTCOME, "3 BUY signals lost after a regime flip",
        client=client,  # type: ignore[arg-type]
        settings=kwargs.get("settings") or make_settings(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_proposal_is_a_gated_draft_with_real_inputs() -> None:
    proposal = await _build(ScriptedClient(DRAFT_JSON, "reflector"))
    assert proposal is not None
    assert proposal.status == "DRAFT" and proposal.requires_human_signoff is True
    assert proposal.required_stages == REQUIRED_STAGES
    assert proposal.trigger_signal_id == "sig-42"
    assert proposal.realised_pnl == -2.1
    assert proposal.regime_at_close == RegimeLabel.HIGH_VOL_CHOP
    assert proposal.debate_history == "bought on momentum, judge sided BUY"
    assert proposal.observed_underperformance == "3 BUY signals lost after a regime flip"
    assert "regime-flip check" in proposal.proposed_change
    assert "did not price in" in proposal.rationale  # the model's real rationale, not a stub


@pytest.mark.asyncio
async def test_proposal_has_no_apply_or_deploy_surface() -> None:
    proposal = await _build(ScriptedClient(DRAFT_JSON, "reflector"))
    assert proposal is not None
    surface = set(RubricChangeProposal.model_fields) | set(dir(proposal))
    assert not surface & {"applied", "deployed", "apply", "deploy", "promote"}
    with pytest.raises(Exception):  # noqa: B017 - frozen model: pydantic ValidationError
        proposal.status = "APPLIED"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_prompt_includes_real_inputs_and_delimits_untrusted_text() -> None:
    client = ScriptedClient(DRAFT_JSON, "reflector")
    await _build(client)
    prompt = client.prompts[0]
    assert "-2.1" in prompt and "HIGH_VOL_CHOP" in prompt
    assert "bought on momentum" in prompt and "<untrusted_output" in prompt


@pytest.mark.asyncio
async def test_timeout_returns_none_never_timeout_text() -> None:
    settings = make_settings(COGNITIVE_REFLECTOR_TIMEOUT_S="0.01")
    assert await _build(HangingClient("reflector"), settings=settings) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "free text, not json",
        "",
        '{"proposed_change": "x"}',
        '{"proposed_change": "", "rationale": "r"}',
        '{"proposed_change": "x", "rationale": "r", "apply": true}',
        '{"proposed_change": "' + "a" * 5000 + '", "rationale": "r"}',
        RuntimeError("provider down"),
        7,
    ],
)
async def test_invalid_or_failing_reply_returns_none(reply: object) -> None:
    assert await _build(ScriptedClient(reply, "reflector")) is None


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    with pytest.raises(asyncio.CancelledError):
        await _build(ScriptedClient(asyncio.CancelledError(), "reflector"))


@pytest.mark.asyncio
async def test_max_tokens_comes_from_reflector_setting_not_compression() -> None:
    seen: list[int] = []

    class _Spy:
        async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
            seen.append(max_tokens)
            return DRAFT_JSON

    settings = make_settings(
        COGNITIVE_REFLECTOR_MAX_TOKENS="777", COGNITIVE_COMPRESSION_MAX_TOKENS="50"
    )
    await _build(_Spy(), settings=settings)
    assert seen == [777]
