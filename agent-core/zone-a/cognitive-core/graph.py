"""LangGraph wiring for the Blue -> Red -> Judge -> Compression debate.

Each node enforces its ADR-002 latency budget via asyncio.wait_for. A budget
breach never raises out of the graph — Blue/Red fall back to a
forced-completion result built from whatever partial state they have, and
Judge falls back to a default-abstain verdict. This mirrors the sensory-array
TTL philosophy from Phase 1: a stale/late signal is handled explicitly, not
allowed to crash the pipeline.
"""

from __future__ import annotations

import asyncio
import uuid

from langgraph.graph import END, StateGraph

import config
import prompts
from llm_clients import (
    LLMClient,
    get_blue_client,
    get_compression_client,
    get_judge_client,
    get_red_client,
)
from models import (
    BlueThesis,
    DebateState,
    JudgeVerdict,
    RedChallenge,
    SignalSide,
    SignalStatus,
    TradeSignal,
)


async def _blue_step(state: DebateState, client: LLMClient) -> dict:
    prompt = prompts.BLUE_SYSTEM_PROMPT.format(
        market_context=state.market_context.model_dump_json(),
        regime=state.regime.value,
        regime_confidence=state.regime_confidence,
    )
    try:
        raw = await asyncio.wait_for(
            client.ainvoke(prompt, max_tokens=config.BLUE_RED_MAX_TOKENS),
            timeout=config.BLUE_LATENCY_BUDGET_S,
        )
        thesis = BlueThesis.model_validate_json(raw)
    except (TimeoutError, asyncio.TimeoutError, ValueError):
        thesis = BlueThesis(
            side=SignalSide.SIDE_UNKNOWN,
            rationale="blue_node latency budget breached — forced completion",
            key_factors=[],
            forced_completion=True,
        )
    return {"blue_thesis": thesis}


async def _red_step(state: DebateState, client: LLMClient) -> dict:
    assert state.blue_thesis is not None
    prompt = prompts.RED_SYSTEM_PROMPT.format(
        blue_thesis=state.blue_thesis.model_dump_json(),
        regime=state.regime.value,
    )
    try:
        raw = await asyncio.wait_for(
            client.ainvoke(prompt, max_tokens=config.BLUE_RED_MAX_TOKENS),
            timeout=config.RED_LATENCY_BUDGET_S,
        )
        challenge = RedChallenge.model_validate_json(raw)
    except (TimeoutError, asyncio.TimeoutError, ValueError):
        challenge = RedChallenge(
            counter_factors=[],
            failure_patterns=["red_node latency budget breached — forced completion"],
            forced_completion=True,
        )
    return {"red_challenge": challenge}


async def _judge_step(state: DebateState, client: LLMClient) -> dict:
    assert state.blue_thesis is not None
    assert state.red_challenge is not None
    prompt = prompts.JUDGE_SYSTEM_PROMPT.format(
        blue_thesis=state.blue_thesis.model_dump_json(),
        red_challenge=state.red_challenge.model_dump_json(),
        regime=state.regime.value,
        regime_confidence=state.regime_confidence,
    )
    try:
        raw = await asyncio.wait_for(
            client.ainvoke(prompt, max_tokens=config.JUDGE_MAX_TOKENS),
            timeout=config.JUDGE_LATENCY_BUDGET_S,
        )
        verdict = JudgeVerdict.model_validate_json(raw)
    except (TimeoutError, asyncio.TimeoutError, ValueError):
        verdict = JudgeVerdict(
            side=SignalSide.SIDE_UNKNOWN,
            omega=0.0,
            p_success=0.0,
            p_failure=1.0,
            reward_estimate=0.0,
            risk_estimate=0.0,
            defaulted_abstain=True,
        )
    return {"judge_verdict": verdict}


async def _compression_step(state: DebateState, client: LLMClient) -> dict:
    assert state.blue_thesis is not None
    assert state.red_challenge is not None
    assert state.judge_verdict is not None
    prompt = prompts.COMPRESSION_SYSTEM_PROMPT.format(
        blue_thesis=state.blue_thesis.model_dump_json(),
        red_challenge=state.red_challenge.model_dump_json(),
        judge_verdict=state.judge_verdict.model_dump_json(),
    )
    try:
        summary = await asyncio.wait_for(
            client.ainvoke(prompt, max_tokens=config.COMPRESSION_MAX_TOKENS),
            timeout=config.COMPRESSION_LATENCY_BUDGET_S,
        )
    except (TimeoutError, asyncio.TimeoutError):
        # Fallback per spec: raw Blue rationale + Red counter factors, same cap.
        fallback = f"{state.blue_thesis.rationale} | {'; '.join(state.red_challenge.counter_factors)}"
        summary = fallback[:800]
    return {"debate_summary": summary}


def build_debate_graph(
    blue_client: LLMClient | None = None,
    red_client: LLMClient | None = None,
    judge_client: LLMClient | None = None,
    compression_client: LLMClient | None = None,
):
    """Wire the graph. Clients are injectable for testing; default to the
    real ADR-002 provider factories otherwise."""

    blue_client = blue_client or get_blue_client()
    red_client = red_client or get_red_client()
    judge_client = judge_client or get_judge_client()
    compression_client = compression_client or get_compression_client()

    async def blue_node(state: DebateState) -> dict:
        return await _blue_step(state, blue_client)

    async def red_node(state: DebateState) -> dict:
        return await _red_step(state, red_client)

    async def judge_node(state: DebateState) -> dict:
        return await _judge_step(state, judge_client)

    async def compression_node(state: DebateState) -> dict:
        return await _compression_step(state, compression_client)

    graph = StateGraph(DebateState)
    graph.add_node("blue", blue_node)
    graph.add_node("red", red_node)
    graph.add_node("judge", judge_node)
    graph.add_node("compression", compression_node)

    graph.set_entry_point("blue")
    graph.add_edge("blue", "red")
    graph.add_edge("red", "judge")
    graph.add_edge("judge", "compression")
    graph.add_edge("compression", END)

    return graph.compile()


def to_trade_signal(state: DebateState) -> TradeSignal:
    """Convert a completed DebateState into the proto-shaped TradeSignal."""
    assert state.judge_verdict is not None
    assert state.debate_summary is not None
    verdict = state.judge_verdict

    status = (
        SignalStatus.SIGNAL_ABSTAIN
        if verdict.defaulted_abstain or verdict.omega < config.OMEGA_THRESHOLD
        else SignalStatus.SIGNAL_PENDING
    )

    return TradeSignal(
        signal_id=str(uuid.uuid4()),
        symbol=state.market_context.symbol,
        side=verdict.side if status != SignalStatus.SIGNAL_ABSTAIN else SignalSide.SIDE_UNKNOWN,
        quantity=0.0,
        omega=verdict.omega,
        expected_value=verdict.reward_estimate - verdict.risk_estimate,
        p_success=verdict.p_success,
        p_failure=verdict.p_failure,
        reward_estimate=verdict.reward_estimate,
        risk_estimate=verdict.risk_estimate,
        regime=state.regime,
        regime_confidence=state.regime_confidence,
        debate_summary=state.debate_summary,
        status=status,
    )


__all__ = ["build_debate_graph", "to_trade_signal"]
