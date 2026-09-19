import asyncio

import pytest

import config
from graph import build_debate_graph, to_trade_signal
from models import DebateState, MarketContext, RegimeLabel, SignalSide, SignalStatus


def _market_context() -> MarketContext:
    return MarketContext(
        symbol="AAPL",
        mid_price=190.0,
        z_score=1.2,
        mad_score=0.9,
        ofi=0.1,
        realized_vol=0.22,
        adv_30d=5_000_000,
    )


def _initial_state() -> DebateState:
    return DebateState(
        market_context=_market_context(),
        regime=RegimeLabel.TRENDING_BULL,
        regime_confidence=0.85,
    )


class _StubClient:
    """Returns a fixed JSON payload immediately — no timeout risk."""

    def __init__(self, payload: str, delay: float = 0.0):
        self._payload = payload
        self._delay = delay

    async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._payload


BLUE_JSON = '{"side": "BUY", "rationale": "strong momentum", "key_factors": ["ofi_positive"]}'
RED_JSON = '{"counter_factors": ["overbought"], "failure_patterns": ["bull_trap_2023"]}'
JUDGE_JSON = (
    '{"side": "BUY", "omega": 0.72, "p_success": 0.65, "p_failure": 0.35, '
    '"reward_estimate": 120.0, "risk_estimate": 40.0}'
)


@pytest.mark.asyncio
async def test_graph_wiring_order_happy_path():
    graph = build_debate_graph(
        blue_client=_StubClient(BLUE_JSON),
        red_client=_StubClient(RED_JSON),
        judge_client=_StubClient(JUDGE_JSON),
        compression_client=_StubClient("Blue bought on momentum; Red flagged overbought; Judge sided BUY at 0.72."),
    )
    result = await graph.ainvoke(_initial_state())

    assert result["blue_thesis"].side == SignalSide.BUY
    assert result["red_challenge"].failure_patterns == ["bull_trap_2023"]
    assert result["judge_verdict"].omega == pytest.approx(0.72)
    assert "overbought" in result["debate_summary"]


@pytest.mark.asyncio
async def test_latency_budget_breach_blue_red_forces_completion():
    graph = build_debate_graph(
        blue_client=_StubClient(BLUE_JSON, delay=config.BLUE_LATENCY_BUDGET_S + 0.5),
        red_client=_StubClient(RED_JSON, delay=config.RED_LATENCY_BUDGET_S + 0.5),
        judge_client=_StubClient(JUDGE_JSON),
        compression_client=_StubClient("summary"),
    )
    result = await graph.ainvoke(_initial_state())

    assert result["blue_thesis"].forced_completion is True
    assert result["blue_thesis"].side == SignalSide.SIDE_UNKNOWN
    assert result["red_challenge"].forced_completion is True


@pytest.mark.asyncio
async def test_latency_budget_breach_judge_defaults_abstain():
    graph = build_debate_graph(
        blue_client=_StubClient(BLUE_JSON),
        red_client=_StubClient(RED_JSON),
        judge_client=_StubClient(JUDGE_JSON, delay=config.JUDGE_LATENCY_BUDGET_S + 0.5),
        compression_client=_StubClient("summary"),
    )
    result = await graph.ainvoke(_initial_state())

    assert result["judge_verdict"].defaulted_abstain is True
    assert result["judge_verdict"].omega == 0.0


@pytest.mark.asyncio
async def test_compression_timeout_falls_back_to_raw_summary():
    graph = build_debate_graph(
        blue_client=_StubClient(BLUE_JSON),
        red_client=_StubClient(RED_JSON),
        judge_client=_StubClient(JUDGE_JSON),
        compression_client=_StubClient("ignored", delay=config.COMPRESSION_LATENCY_BUDGET_S + 0.5),
    )
    result = await graph.ainvoke(_initial_state())

    assert "strong momentum" in result["debate_summary"]
    assert "overbought" in result["debate_summary"]


@pytest.mark.asyncio
async def test_abstain_below_omega_threshold():
    low_confidence_judge = (
        '{"side": "BUY", "omega": 0.2, "p_success": 0.3, "p_failure": 0.7, '
        '"reward_estimate": 10.0, "risk_estimate": 40.0}'
    )
    graph = build_debate_graph(
        blue_client=_StubClient(BLUE_JSON),
        red_client=_StubClient(RED_JSON),
        judge_client=_StubClient(low_confidence_judge),
        compression_client=_StubClient("summary"),
    )
    result = await graph.ainvoke(_initial_state())
    final_state = DebateState(**result)
    signal = to_trade_signal(final_state)

    assert signal.status == SignalStatus.SIGNAL_ABSTAIN
    assert signal.side == SignalSide.SIDE_UNKNOWN
