from __future__ import annotations

import asyncio
from typing import Any

import pytest

from cognitive_core.graph import (
    _blue_step,
    _compression_step,
    _judge_step,
    _red_step,
    build_debate_graph,
    run_debate,
)
from cognitive_core.models import (
    BlueThesis,
    RedChallenge,
    DebateState,
    JudgeVerdict,
    SignalSide,
    SignalStatus,
)
from cognitive_core.tests.fakes import (
    BLUE_JSON,
    BLUE_UNKNOWN_JSON,
    JUDGE_JSON,
    RED_JSON,
    SUMMARY,
    HangingClient,
    ScriptedClient,
    initial_state,
    make_settings,
    tiny_budget_settings,
)


def _graph(
    settings: Any,
    *,
    blue: Any = None,
    red: Any = None,
    judge: Any = None,
    compression: Any = None,
    calls: list[str] | None = None,
) -> Any:
    calls = calls if calls is not None else []
    return build_debate_graph(
        settings,
        blue_client=blue or ScriptedClient(BLUE_JSON, "blue", calls),
        red_client=red or ScriptedClient(RED_JSON, "red", calls),
        judge_client=judge or ScriptedClient(JUDGE_JSON, "judge", calls),
        compression_client=compression or ScriptedClient(SUMMARY, "compression", calls),
    )


async def _run(graph: Any) -> DebateState:
    return DebateState.model_validate(await graph.ainvoke(initial_state()))


@pytest.mark.asyncio
async def test_wiring_order_is_blue_red_judge_compression() -> None:
    calls: list[str] = []
    result = await _run(_graph(make_settings(), calls=calls))
    assert calls == ["blue", "red", "judge", "compression"]
    assert result.blue_thesis and result.blue_thesis.side == SignalSide.BUY
    assert result.red_challenge and result.red_challenge.failure_patterns == ("bull_trap_2023",)
    assert result.judge_verdict and result.judge_verdict.omega == pytest.approx(0.72)
    assert result.debate_summary == SUMMARY


@pytest.mark.asyncio
async def test_prompts_carry_schema_and_untrusted_delimiters() -> None:
    blue = ScriptedClient(BLUE_JSON, "blue")
    red = ScriptedClient(RED_JSON, "red")
    judge = ScriptedClient(JUDGE_JSON, "judge")
    await _run(_graph(make_settings(), blue=blue, red=red, judge=judge))
    assert "ONE JSON object" in blue.prompts[0]
    assert "<untrusted_output" in red.prompts[0] and "strong momentum" in red.prompts[0]
    assert "<untrusted_output" in judge.prompts[0] and "bull_trap_2023" in judge.prompts[0]


@pytest.mark.asyncio
async def test_blue_and_red_timeouts_force_completion_without_raising() -> None:
    calls: list[str] = []
    graph = _graph(
        tiny_budget_settings(),
        blue=HangingClient("blue", calls),
        red=HangingClient("red", calls),
        calls=calls,
    )
    result = await _run(graph)
    assert result.blue_thesis and result.blue_thesis.forced_completion
    assert result.blue_thesis.side == SignalSide.SIDE_UNKNOWN
    assert result.red_challenge and result.red_challenge.forced_completion


@pytest.mark.asyncio
async def test_forced_blue_skips_red_and_judge_and_abstains() -> None:
    """Judge must not be able to turn a fully forced debate into a BUY."""
    calls: list[str] = []
    settings = tiny_budget_settings()
    graph = _graph(settings, blue=HangingClient("blue", calls), calls=calls)
    result = await _run(graph)
    assert "red" not in calls and "judge" not in calls
    assert result.judge_verdict == JudgeVerdict.default_abstain()


@pytest.mark.asyncio
async def test_unknown_blue_side_skips_judge() -> None:
    calls: list[str] = []
    graph = _graph(
        make_settings(), blue=ScriptedClient(BLUE_UNKNOWN_JSON, "blue", calls), calls=calls
    )
    result = await _run(graph)
    assert "judge" not in calls
    assert result.judge_verdict and result.judge_verdict.defaulted_abstain


@pytest.mark.asyncio
async def test_forced_red_caps_judge_omega_below_threshold() -> None:
    settings = tiny_budget_settings()
    graph = _graph(settings, red=HangingClient("red"))
    result = await _run(graph)
    assert result.red_challenge and result.red_challenge.forced_completion
    assert result.judge_verdict
    assert result.judge_verdict.omega < settings.omega_threshold
    assert result.judge_verdict.omega <= settings.omega_threshold * 0.5


@pytest.mark.asyncio
async def test_judge_timeout_defaults_to_abstain_signal() -> None:
    settings = tiny_budget_settings()
    graph = _graph(settings, judge=HangingClient("judge"))
    signal = await run_debate(graph, initial_state(), settings=settings, quantity=10.0)
    assert signal.status == SignalStatus.SIGNAL_ABSTAIN
    assert signal.side == SignalSide.SIDE_UNKNOWN and signal.quantity == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "",
        "{}",
        '{"side": "BUY"',
        '{"side": "MOON", "rationale": "r"}',
        '{"side": "BUY", "rationale": "r", "surprise": 1}',
        RuntimeError("provider exploded"),
        ConnectionError("network down"),
        KeyError("boom"),
        12345,
        None,
    ],
)
async def test_blue_failures_of_any_kind_fail_closed(reply: object) -> None:
    graph = _graph(make_settings(), blue=ScriptedClient(reply, "blue"))
    result = await _run(graph)
    assert result.blue_thesis and result.blue_thesis.forced_completion
    assert result.judge_verdict and result.judge_verdict.defaulted_abstain


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "garbage",
        '{"side": "BUY", "omega": 0.9, "p_success": 0.9, "p_failure": 0.9, '
        '"reward_estimate": 1, "risk_estimate": 1}',
        '{"side": "BUY", "omega": 2, "p_success": 0.5, "p_failure": 0.5, '
        '"reward_estimate": 1, "risk_estimate": 1}',
        '{"side": "BUY", "omega": 0.9, "p_success": 0.5, "p_failure": 0.5, '
        '"reward_estimate": -1, "risk_estimate": 1}',
        ValueError("bad"),
        OSError("io"),
    ],
)
async def test_judge_invalid_or_failing_reply_defaults_to_abstain(reply: object) -> None:
    graph = _graph(make_settings(), judge=ScriptedClient(reply, "judge"))
    result = await _run(graph)
    assert result.judge_verdict == JudgeVerdict.default_abstain()


@pytest.mark.asyncio
async def test_red_non_timeout_exception_forces_completion() -> None:
    graph = _graph(make_settings(), red=ScriptedClient(RuntimeError("x"), "red"))
    result = await _run(graph)
    assert result.red_challenge and result.red_challenge.forced_completion


@pytest.mark.asyncio
async def test_json_code_fence_is_accepted_but_validation_stays_strict() -> None:
    fenced = f"```json\n{BLUE_JSON}\n```"
    result = await _run(_graph(make_settings(), blue=ScriptedClient(fenced, "blue")))
    assert result.blue_thesis and not result.blue_thesis.forced_completion


@pytest.mark.asyncio
async def test_compression_timeout_falls_back_to_raw_summary() -> None:
    graph = _graph(tiny_budget_settings(), compression=HangingClient("compression"))
    result = await _run(graph)
    assert result.debate_summary and "strong momentum" in result.debate_summary
    assert "overbought" in result.debate_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["", "   ", RuntimeError("x"), 5])
async def test_compression_invalid_output_falls_back(reply: object) -> None:
    graph = _graph(make_settings(), compression=ScriptedClient(reply, "compression"))
    result = await _run(graph)
    assert result.debate_summary and "strong momentum" in result.debate_summary


@pytest.mark.asyncio
async def test_compression_output_is_truncated_to_cap() -> None:
    settings = make_settings(COGNITIVE_COMPRESSION_MAX_TOKENS="10")
    graph = _graph(settings, compression=ScriptedClient("y" * 5000, "compression"))
    result = await _run(graph)
    assert result.debate_summary == "y" * 40


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_by_any_node() -> None:
    # (LangGraph itself re-wraps a node-raised CancelledError, so test the node steps.)
    settings = make_settings()
    cancelled = ScriptedClient(asyncio.CancelledError(), "x")
    ready = initial_state().model_copy(
        update={
            "blue_thesis": BlueThesis(side=SignalSide.BUY, rationale="r"),
            "red_challenge": RedChallenge(counter_factors=("c",)),
            "judge_verdict": JudgeVerdict.default_abstain(),
        }
    )
    for step in (_blue_step, _red_step, _judge_step, _compression_step):
        with pytest.raises(asyncio.CancelledError):
            await step(ready, cancelled, settings)


@pytest.mark.asyncio
async def test_overall_deadline_caps_node_timeout_and_later_nodes_fail_closed() -> None:
    # Blue's own budget is 0.75 s but the overall deadline is 50 ms: the node is cut at
    # the deadline, and Red/Judge are skipped as Blue is forced.
    settings = make_settings(COGNITIVE_OVERALL_DEADLINE_S="0.05")
    calls: list[str] = []
    graph = _graph(settings, blue=HangingClient("blue", calls), calls=calls)
    result = await _run(graph)
    assert result.blue_thesis and result.blue_thesis.forced_completion
    assert calls == ["blue"]


@pytest.mark.asyncio
async def test_exhausted_deadline_makes_no_llm_calls_at_all() -> None:
    calls: list[str] = []
    graph = _graph(make_settings(), calls=calls)
    state = initial_state().model_copy(update={"debate_started_at": -1000.0})
    result = DebateState.model_validate(await graph.ainvoke(state))
    assert calls == []
    assert result.blue_thesis and result.blue_thesis.forced_completion
    assert result.judge_verdict and result.judge_verdict.defaulted_abstain


@pytest.mark.asyncio
async def test_run_debate_hard_deadline_returns_abstain() -> None:
    settings = make_settings(COGNITIVE_OVERALL_DEADLINE_S="0.05")

    class _Graph:
        async def ainvoke(self, state: DebateState) -> dict[str, Any]:
            await asyncio.Event().wait()
            return {}

    signal = await run_debate(_Graph(), initial_state(), settings=settings, quantity=5.0)
    assert signal.status == SignalStatus.SIGNAL_ABSTAIN
    assert "abort" in signal.debate_summary


@pytest.mark.asyncio
async def test_run_debate_graph_exception_returns_abstain() -> None:
    class _Boom:
        async def ainvoke(self, state: DebateState) -> dict[str, Any]:
            raise RuntimeError("graph blew up")

    signal = await run_debate(_Boom(), initial_state(), settings=make_settings(), quantity=5.0)
    assert signal.status == SignalStatus.SIGNAL_ABSTAIN


@pytest.mark.asyncio
async def test_run_debate_happy_path_is_pending_with_expiry() -> None:
    settings = make_settings()
    signal = await run_debate(_graph(settings), initial_state(), settings=settings, quantity=10.0)
    assert signal.status == SignalStatus.SIGNAL_PENDING
    assert signal.side == SignalSide.BUY and signal.quantity == 10.0
    assert signal.valid_until_ns - signal.created_at_ns == settings.signal_ttl_ms * 1_000_000
    # E[V] = p_success * reward - p_failure * risk = 0.65*120 - 0.35*40
    assert signal.expected_value == pytest.approx(64.0)


@pytest.mark.asyncio
async def test_run_debate_without_sizer_quantity_abstains() -> None:
    settings = make_settings()
    signal = await run_debate(_graph(settings), initial_state(), settings=settings)
    assert signal.status == SignalStatus.SIGNAL_ABSTAIN


@pytest.mark.asyncio
async def test_low_omega_abstains() -> None:
    low = (
        '{"side": "BUY", "omega": 0.2, "p_success": 0.3, "p_failure": 0.7, '
        '"reward_estimate": 10.0, "risk_estimate": 40.0}'
    )
    settings = make_settings()
    graph = _graph(settings, judge=ScriptedClient(low, "judge"))
    signal = await run_debate(graph, initial_state(), settings=settings, quantity=10.0)
    assert signal.status == SignalStatus.SIGNAL_ABSTAIN
    assert signal.side == SignalSide.SIDE_UNKNOWN
