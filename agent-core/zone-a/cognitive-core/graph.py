"""LangGraph wiring for the Blue -> Red -> Judge -> Compression debate.

Fail-closed design: ANY exception inside a node (timeout, provider error, malformed or
invalid reply, missing upstream state) is logged with structlog and degraded to
forced-completion / default-abstain output; only `asyncio.CancelledError` propagates.
Each node's timeout is `min(its ADR-002 budget, time left before the overall
deadline)`, and `run_debate` additionally enforces the overall deadline as a hard
wall-clock cap around the whole graph (spec: Blue+Red < 1.5 s, total 2.2 s).

Degraded debates never trade: if Blue is forced/UNKNOWN, Red and Judge are skipped;
if Red is forced, the Judge's omega is capped strictly below the abstain threshold.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from langgraph.graph import END, StateGraph

from . import prompts
from .config import CognitiveSettings, load_settings
from .llm_clients import (
    LLMClient,
    get_blue_client,
    get_compression_client,
    get_judge_client,
    get_red_client,
)
from .models import (
    BlueThesis,
    DebateState,
    JudgeVerdict,
    RedChallenge,
    SignalSide,
    TradeSignal,
    truncate_summary,
)
from .parsing import ModelOutputError, parse_json_model
from .signals import NO_SUMMARY, abstain_signal, to_trade_signal

log = structlog.get_logger(__name__)

# A degraded debate's omega is capped at this fraction of the abstain threshold, keeping it
# strictly below the threshold whatever the Judge claims.
DEGRADED_OMEGA_FACTOR = 0.5
_LOG_ERROR_CHARS = 200


def _remaining_s(state: DebateState, settings: CognitiveSettings) -> float:
    if state.debate_started_at is None:
        return settings.overall_deadline_s
    return state.debate_started_at + settings.overall_deadline_s - time.monotonic()


async def _call(
    client: LLMClient, prompt: str, *, max_tokens: int, budget_s: float, state: DebateState,
    settings: CognitiveSettings,
) -> str:
    timeout_s = min(budget_s, _remaining_s(state, settings))
    if timeout_s <= 0:
        raise TimeoutError("overall debate deadline exhausted")
    reply = await asyncio.wait_for(client.ainvoke(prompt, max_tokens=max_tokens), timeout=timeout_s)
    if not isinstance(reply, str):
        raise ModelOutputError(f"client returned {type(reply).__name__}, expected str")
    return reply


def _log_failure(node: str, exc: BaseException, state: DebateState) -> None:
    log.warning(
        "debate_node_failed",
        node=node,
        error_type=type(exc).__name__,
        error=str(exc)[:_LOG_ERROR_CHARS],
        symbol=state.market_context.symbol,
        outcome="degraded_fail_closed",
    )


def _blue_unusable(blue: BlueThesis | None) -> bool:
    return blue is None or blue.forced_completion or blue.side == SignalSide.SIDE_UNKNOWN


async def _blue_step(
    state: DebateState, client: LLMClient, settings: CognitiveSettings
) -> dict[str, Any]:
    started = state.debate_started_at if state.debate_started_at is not None else time.monotonic()
    timed = state.model_copy(update={"debate_started_at": started})
    prompt = prompts.BLUE_SYSTEM_PROMPT.format(
        market_context=state.market_context.model_dump_json(),
        regime=state.regime.value,
        regime_confidence=state.regime_confidence,
    )
    try:
        raw = await _call(
            client, prompt, max_tokens=settings.blue_red_max_tokens,
            budget_s=settings.blue_latency_budget_s, state=timed, settings=settings,
        )
        thesis = parse_json_model(raw, BlueThesis)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on every node failure
        _log_failure("blue", exc, state)
        thesis = BlueThesis(
            side=SignalSide.SIDE_UNKNOWN,
            rationale=f"blue_node failed ({type(exc).__name__}) - forced completion",
            forced_completion=True,
        )
    return {"debate_started_at": started, "blue_thesis": thesis}


async def _red_step(
    state: DebateState, client: LLMClient, settings: CognitiveSettings
) -> dict[str, Any]:
    if _blue_unusable(state.blue_thesis) or state.blue_thesis is None:
        log.info("red_skipped", reason="blue_forced_or_unknown", symbol=state.market_context.symbol)
        return {
            "red_challenge": RedChallenge(
                failure_patterns=("red_node skipped: no usable Blue thesis",),
                forced_completion=True,
            )
        }
    prompt = prompts.RED_SYSTEM_PROMPT.format(
        blue_thesis=prompts.wrap_untrusted("blue", state.blue_thesis.model_dump_json()),
        regime=state.regime.value,
    )
    try:
        raw = await _call(
            client, prompt, max_tokens=settings.blue_red_max_tokens,
            budget_s=settings.red_latency_budget_s, state=state, settings=settings,
        )
        challenge = parse_json_model(raw, RedChallenge)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on every node failure
        _log_failure("red", exc, state)
        challenge = RedChallenge(
            failure_patterns=(f"red_node failed ({type(exc).__name__}) - forced completion",),
            forced_completion=True,
        )
    return {"red_challenge": challenge}


def _cap_degraded(verdict: JudgeVerdict, settings: CognitiveSettings) -> JudgeVerdict:
    cap = settings.omega_threshold * DEGRADED_OMEGA_FACTOR
    if verdict.omega <= cap:
        return verdict
    return verdict.model_copy(update={"omega": cap})


async def _judge_step(
    state: DebateState, client: LLMClient, settings: CognitiveSettings
) -> dict[str, Any]:
    blue, red = state.blue_thesis, state.red_challenge
    if blue is None or red is None or _blue_unusable(blue):
        log.info("judge_skipped", reason="no_usable_blue", symbol=state.market_context.symbol)
        return {"judge_verdict": JudgeVerdict.default_abstain()}
    prompt = prompts.JUDGE_SYSTEM_PROMPT.format(
        blue_thesis=prompts.wrap_untrusted("blue", blue.model_dump_json()),
        red_challenge=prompts.wrap_untrusted("red", red.model_dump_json()),
        regime=state.regime.value,
        regime_confidence=state.regime_confidence,
    )
    try:
        raw = await _call(
            client, prompt, max_tokens=settings.judge_max_tokens,
            budget_s=settings.judge_latency_budget_s, state=state, settings=settings,
        )
        verdict = parse_json_model(raw, JudgeVerdict)
        if red.forced_completion:
            verdict = _cap_degraded(verdict, settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on every node failure
        _log_failure("judge", exc, state)
        verdict = JudgeVerdict.default_abstain()
    return {"judge_verdict": verdict}


def _fallback_summary(state: DebateState, settings: CognitiveSettings) -> str:
    """Spec fallback: Blue rationale[:200] + Red counter_factors[:200], never empty."""
    blue, red = state.blue_thesis, state.red_challenge
    rationale = blue.rationale[:200] if blue else ""
    counters = "; ".join(red.counter_factors)[:200] if red else ""
    text = f"{rationale} | {counters}".strip(" |")
    return truncate_summary(text or NO_SUMMARY, settings.compression_max_tokens)


async def _compression_step(
    state: DebateState, client: LLMClient, settings: CognitiveSettings
) -> dict[str, Any]:
    if state.blue_thesis is None or state.red_challenge is None or state.judge_verdict is None:
        return {"debate_summary": _fallback_summary(state, settings)}
    prompt = prompts.COMPRESSION_SYSTEM_PROMPT.format(
        max_tokens=settings.compression_max_tokens,
        blue_thesis=prompts.wrap_untrusted("blue", state.blue_thesis.model_dump_json()),
        red_challenge=prompts.wrap_untrusted("red", state.red_challenge.model_dump_json()),
        judge_verdict=prompts.wrap_untrusted("judge", state.judge_verdict.model_dump_json()),
    )
    try:
        summary = await _call(
            client, prompt, max_tokens=settings.compression_max_tokens,
            budget_s=settings.compression_latency_budget_s, state=state, settings=settings,
        )
        if not summary.strip():
            raise ModelOutputError("empty compression summary")
        summary = truncate_summary(summary.strip(), settings.compression_max_tokens)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on every node failure
        _log_failure("compression", exc, state)
        summary = _fallback_summary(state, settings)
    return {"debate_summary": summary}


def build_debate_graph(
    settings: CognitiveSettings | None = None,
    blue_client: LLMClient | None = None,
    red_client: LLMClient | None = None,
    judge_client: LLMClient | None = None,
    compression_client: LLMClient | None = None,
) -> Any:
    """Wire the graph. Clients are injectable for tests; default to the Bedrock factories."""
    cfg = settings or load_settings()
    blue = blue_client or get_blue_client(cfg)
    red = red_client or get_red_client(cfg)
    judge = judge_client or get_judge_client(cfg)
    compression = compression_client or get_compression_client(cfg)

    async def blue_node(state: DebateState) -> dict[str, Any]:
        return await _blue_step(state, blue, cfg)

    async def red_node(state: DebateState) -> dict[str, Any]:
        return await _red_step(state, red, cfg)

    async def judge_node(state: DebateState) -> dict[str, Any]:
        return await _judge_step(state, judge, cfg)

    async def compression_node(state: DebateState) -> dict[str, Any]:
        return await _compression_step(state, compression, cfg)

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


async def run_debate(
    graph: Any,
    initial_state: DebateState,
    *,
    settings: CognitiveSettings,
    quantity: float = 0.0,
) -> TradeSignal:
    """Run the graph under a hard overall deadline; any failure yields an ABSTAIN signal."""
    try:
        result = await asyncio.wait_for(
            graph.ainvoke(initial_state), timeout=settings.overall_deadline_s
        )
        final = result if isinstance(result, DebateState) else DebateState.model_validate(result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on every graph failure
        _log_failure("graph", exc, initial_state)
        return abstain_signal(
            initial_state, settings=settings,
            summary=f"debate aborted ({type(exc).__name__}) - abstain",
        )
    return to_trade_signal(final, settings=settings, quantity=quantity)


__all__ = ["build_debate_graph", "run_debate", "to_trade_signal"]
