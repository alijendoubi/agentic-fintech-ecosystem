"""cognitive-core wired to the real afe_vector_memory layer (in-memory store, no server)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from cognitive_core.service import CycleOutcome
from cognitive_core.tests.runner_fakes import make_harness, runner_settings, snapshot_json

VECTOR_DB_DIR = Path(__file__).resolve().parents[2] / "vector-db"


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch) -> Any:
    if not (VECTOR_DB_DIR / "afe_vector_memory").is_dir():
        pytest.skip("vector-db package not present")
    monkeypatch.syspath_prepend(str(VECTOR_DB_DIR))
    import afe_vector_memory as avm

    store = avm.InMemoryStore()
    return avm, store, avm.AsyncMemoryAdapter(store, top_k=3, timeout_s=2.0)


@pytest.mark.asyncio
async def test_debate_outcome_written_then_recalled_on_the_next_cycle(memory: Any) -> None:
    _, store, adapter = memory
    h = make_harness(runner_settings(), precedents=adapter, reflections=adapter)
    assert await h.runner.handle_message(snapshot_json()) is CycleOutcome.EMITTED
    assert store.count() == 1
    assert await h.runner.handle_message(snapshot_json(symbol="MSFT")) is CycleOutcome.EMITTED
    prompt = h.clients["judge"].prompts[-1]
    assert "Similar past debates" in prompt and "[pending]" in prompt
    assert "regime=TRENDING_BULL" in prompt


@pytest.mark.asyncio
async def test_store_outage_is_explicit_and_fails_closed(memory: Any) -> None:
    _, store, adapter = memory
    store.fail_with = ConnectionError("chroma down")
    h = make_harness(runner_settings(), precedents=adapter, reflections=adapter)
    assert await h.runner.handle_message(snapshot_json()) is CycleOutcome.SKIPPED_MEMORY
    assert h.sink.sent == []
    optional = make_harness(
        runner_settings(COGNITIVE_MEMORY_REQUIRED="false"), precedents=adapter, reflections=adapter
    )
    assert await optional.runner.handle_message(snapshot_json()) is CycleOutcome.EMITTED
    judge_prompt = optional.clients["judge"].prompts[0]
    assert "UNAVAILABLE" in judge_prompt
    assert len(optional.sink.sent) == 1  # signal still delivered; the failed write is only logged


@pytest.mark.asyncio
async def test_empty_but_reachable_store_says_no_precedent_found(memory: Any) -> None:
    _, _, adapter = memory
    h = make_harness(runner_settings(), precedents=adapter)
    await h.runner.handle_message(snapshot_json())
    assert "no similar past debates found" in h.clients["judge"].prompts[0]
