from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from cognitive_core.models import SignalSide, SignalStatus
from cognitive_core.service import CognitiveRunner, CycleOutcome, SourceError
from cognitive_core.sinks import InMemorySink
from cognitive_core.sources import StaticSource
from cognitive_core.tests.fakes import BLUE_JSON, RED_JSON, ScriptedClient
from cognitive_core.tests.runner_fakes import (
    FakeProvider,
    FakeWriter,
    Harness,
    make_harness,
    runner_settings,
    snapshot,
    snapshot_json,
)

ONE_MOVE_UP = 0.72  # JUDGE_JSON omega


def _precedent(text: str = "breakout failed") -> SimpleNamespace:
    return SimpleNamespace(
        text=text, outcome="loss", regime="TRENDING_BULL", symbol="MSFT", distance=0.12
    )


async def _handle(h: Harness, raw: str | bytes | None = None) -> CycleOutcome:
    return await h.runner.handle_message(snapshot_json() if raw is None else raw)


@pytest.mark.asyncio
async def test_happy_path_emits_a_pending_signal_to_the_sink() -> None:
    h = make_harness()
    assert await _handle(h) is CycleOutcome.EMITTED
    (signal,) = h.sink.sent
    assert signal.status is SignalStatus.SIGNAL_PENDING
    assert signal.side is SignalSide.BUY
    assert signal.symbol == "AAPL"
    assert signal.quantity == 10.0
    assert signal.omega == pytest.approx(ONE_MOVE_UP)
    assert h.calls == ["blue", "red", "judge", "compression"]
    assert h.health.snapshot()[1]["counters"]["emitted"] == 1


@pytest.mark.asyncio
async def test_default_quantity_zero_always_abstains_and_sends_nothing() -> None:
    h = make_harness(runner_settings(COGNITIVE_ORDER_QUANTITY="0"))
    assert await _handle(h) is CycleOutcome.ABSTAINED
    assert h.sink.sent == []


@pytest.mark.asyncio
async def test_emit_abstain_sends_the_abstain_signal_too() -> None:
    h = make_harness(runner_settings(COGNITIVE_ORDER_QUANTITY="0", COGNITIVE_EMIT_ABSTAIN="true"))
    assert await _handle(h) is CycleOutcome.ABSTAINED
    assert [s.status for s in h.sink.sent] == [SignalStatus.SIGNAL_ABSTAIN]


@pytest.mark.asyncio
async def test_bad_judge_reply_degrades_to_abstain_not_a_signal() -> None:
    h = make_harness(judge_reply="not json")
    assert await _handle(h) is CycleOutcome.ABSTAINED
    assert h.sink.sent == []


# -- halt ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_halt_env_blocks_everything_before_any_llm_call() -> None:
    h = make_harness(env={"COGNITIVE_HALT": "1"})
    assert await _handle(h) is CycleOutcome.HALTED
    assert h.calls == [] and h.sink.sent == []
    assert h.health.snapshot()[1]["halted"] is True


@pytest.mark.asyncio
async def test_unparsable_halt_value_halts_fail_closed() -> None:
    h = make_harness(env={"COGNITIVE_HALT": "perhaps"})
    assert await _handle(h) is CycleOutcome.HALTED
    assert h.sink.sent == []


@pytest.mark.asyncio
async def test_halt_file_created_mid_debate_discards_the_signal(tmp_path: Path) -> None:
    halt_file = tmp_path / "halt"

    class HaltingClient(ScriptedClient):
        async def ainvoke(self, prompt: str, *, max_tokens: int) -> str:
            halt_file.touch()  # operator flips the switch while the debate runs
            return await super().ainvoke(prompt, max_tokens=max_tokens)

    h = make_harness(
        env={"COGNITIVE_HALT_FILE": str(halt_file)},
        client_overrides={"blue": HaltingClient(BLUE_JSON, "blue")},
    )
    assert await _handle(h) is CycleOutcome.HALTED
    assert h.sink.sent == []
    assert h.clients["blue"].calls == ["blue"]  # the debate ran; its result was discarded


@pytest.mark.asyncio
async def test_lifting_the_halt_resumes_signals(tmp_path: Path) -> None:
    halt_file = tmp_path / "halt"
    halt_file.touch()
    h = make_harness(env={"COGNITIVE_HALT_FILE": str(halt_file)})
    assert await _handle(h) is CycleOutcome.HALTED
    halt_file.unlink()
    assert await _handle(h) is CycleOutcome.EMITTED
    assert h.health.snapshot()[1]["halted"] is False


# -- parse / filter -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        b"\xff\xfe",
        "[]",
        snapshot_json(mid_price=-1.0),
        snapshot_json(regime_label="REGIME_UNKNOWN"),
        snapshot_json(is_stale=True),
    ],
)
async def test_invalid_messages_are_dropped_without_any_debate(raw: str | bytes) -> None:
    h = make_harness()
    assert await _handle(h, raw) is CycleOutcome.DROPPED_INVALID
    assert h.calls == [] and h.sink.sent == []


@pytest.mark.asyncio
async def test_symbol_allowlist_skips_other_symbols() -> None:
    h = make_harness(runner_settings(COGNITIVE_SYMBOLS="MSFT,NVDA"))
    assert await _handle(h) is CycleOutcome.SKIPPED_SYMBOL
    assert await _handle(h, snapshot_json(symbol="MSFT")) is CycleOutcome.EMITTED


@pytest.mark.asyncio
async def test_per_symbol_cooldown_limits_debates() -> None:
    h = make_harness(runner_settings(COGNITIVE_MIN_DEBATE_INTERVAL_S="60"))
    assert await _handle(h) is CycleOutcome.EMITTED
    assert await _handle(h) is CycleOutcome.SKIPPED_COOLDOWN
    assert await _handle(h, snapshot_json(symbol="MSFT")) is CycleOutcome.EMITTED  # other symbol
    h.monotonic[0] = 61.0
    assert await _handle(h) is CycleOutcome.EMITTED
    assert len(h.sink.sent) == 3


# -- sink ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_failure_is_counted_not_retried_and_not_recorded() -> None:
    writer = FakeWriter()
    sink = InMemorySink(fail_with=ConnectionError("aegis down"))
    h = make_harness(sink=sink, reflections=writer)
    assert await _handle(h) is CycleOutcome.SINK_FAILED
    assert sink.sent == []
    assert writer.debates == []  # an undelivered signal must not become memory
    assert h.calls.count("judge") == 1  # no retry


@pytest.mark.asyncio
async def test_unexpected_exception_becomes_error_outcome_and_no_signal() -> None:
    class Exploding(InMemorySink):
        async def send(self, signal):  # type: ignore[no-untyped-def, override]
            raise RuntimeError("bug")

    h = make_harness(sink=Exploding())
    assert await _handle(h) is CycleOutcome.ERROR


# -- memory -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_precedents_reach_the_red_and_judge_prompts() -> None:
    provider = FakeProvider([_precedent("fade the breakout at resistance")])
    h = make_harness(precedents=provider)
    assert await _handle(h) is CycleOutcome.EMITTED
    for node in ("red", "judge"):
        prompt = h.clients[node].prompts[0]
        assert "fade the breakout at resistance" in prompt
        assert '<untrusted_output source="memory">' in prompt
    assert provider.requests[0]["regime"] == "TRENDING_BULL"
    assert provider.requests[0]["limit"] == 3
    assert "AAPL TRENDING_BULL" in provider.requests[0]["query_text"]


@pytest.mark.asyncio
async def test_empty_recall_is_stated_as_no_precedent_found() -> None:
    h = make_harness(precedents=FakeProvider([]))
    await _handle(h)
    assert "no similar past debates found" in h.clients["judge"].prompts[0]


@pytest.mark.asyncio
async def test_memory_outage_when_required_skips_the_debate_fail_closed() -> None:
    h = make_harness(precedents=FakeProvider(error=ConnectionError("chroma down")))
    assert await _handle(h) is CycleOutcome.SKIPPED_MEMORY
    assert h.calls == [] and h.sink.sent == []


@pytest.mark.asyncio
async def test_memory_outage_when_optional_is_explicit_in_the_prompt_never_no_precedent() -> None:
    h = make_harness(
        runner_settings(COGNITIVE_MEMORY_REQUIRED="false"),
        precedents=FakeProvider(error=TimeoutError("slow")),
    )
    assert await _handle(h) is CycleOutcome.EMITTED
    prompt = h.clients["judge"].prompts[0]
    assert "UNAVAILABLE" in prompt and "Do NOT assume there is no precedent" in prompt
    assert "no similar past debates found" not in prompt


@pytest.mark.asyncio
async def test_debate_outcome_is_written_after_the_decision() -> None:
    writer = FakeWriter()
    h = make_harness(reflections=writer)
    await _handle(h)
    (record,) = writer.debates
    assert record["symbol"] == "AAPL" and record["regime"] == "TRENDING_BULL"
    assert record["outcome"] == "pending"
    assert record["signal_id"] == h.sink.sent[0].signal_id
    assert record["ts_ms"] == h.sink.sent[0].created_at_ns // 1_000_000


@pytest.mark.asyncio
async def test_abstain_is_recorded_as_abstain_even_though_not_sent() -> None:
    writer = FakeWriter()
    h = make_harness(runner_settings(COGNITIVE_ORDER_QUANTITY="0"), reflections=writer)
    assert await _handle(h) is CycleOutcome.ABSTAINED
    assert [d["outcome"] for d in writer.debates] == ["abstain"]


@pytest.mark.asyncio
async def test_writer_failure_never_blocks_or_changes_the_signal() -> None:
    h = make_harness(reflections=FakeWriter(error=ConnectionError("chroma down")))
    assert await _handle(h) is CycleOutcome.EMITTED
    assert len(h.sink.sent) == 1


# -- the loop -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_processes_a_finite_source_then_returns() -> None:
    h = make_harness()
    source = StaticSource([snapshot_json(), "garbage", snapshot_json(symbol="MSFT")])
    await asyncio.wait_for(h.runner.run(source, asyncio.Event()), timeout=5)
    assert [s.symbol for s in h.sink.sent] == ["AAPL", "MSFT"]
    counters = h.health.snapshot()[1]["counters"]
    assert counters["dropped_invalid"] == 1
    healthy, body = h.health.snapshot()
    assert not healthy and body["status"] == "unhealthy"  # stopped runners are unhealthy


@pytest.mark.asyncio
async def test_run_stops_when_the_stop_event_is_set_and_ticks_the_heartbeat() -> None:
    h = make_harness()
    stop = asyncio.Event()

    class Idle:
        async def messages(self) -> AsyncIterator[str | bytes]:
            await asyncio.Event().wait()
            yield ""  # pragma: no cover

    task = asyncio.create_task(h.runner.run(Idle(), stop))
    await asyncio.sleep(0.2)
    assert h.health.snapshot()[0] is True  # beating while idle
    stop.set()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_source_failure_is_fatal() -> None:
    h = make_harness()

    class Broken:
        async def messages(self) -> AsyncIterator[str | bytes]:
            yield snapshot_json()
            raise ConnectionError("redis gone")

    with pytest.raises(SourceError, match="redis gone"):
        await asyncio.wait_for(h.runner.run(Broken(), asyncio.Event()), timeout=5)
    assert len(h.sink.sent) == 1  # what arrived before the failure was still handled


@pytest.mark.asyncio
async def test_backlog_is_dropped_oldest_first() -> None:
    h = make_harness(runner_settings(COGNITIVE_MIN_DEBATE_INTERVAL_S="0"))
    release = asyncio.Event()

    class Burst:
        async def messages(self) -> AsyncIterator[str | bytes]:
            for i in range(200):
                yield snapshot_json(symbol=f"S{i}")
            release.set()

    await asyncio.wait_for(h.runner.run(Burst(), asyncio.Event()), timeout=10)
    assert release.is_set()
    assert h.health.snapshot()[1]["counters"].get("queue_dropped", 0) > 0
    assert h.sink.sent[-1].symbol == "S199"  # newest survives


def test_runner_is_constructible_with_defaults_only() -> None:
    h = make_harness()
    assert isinstance(h.runner, CognitiveRunner)
    assert BLUE_JSON and RED_JSON
    assert snapshot()["symbol"] == "AAPL"
