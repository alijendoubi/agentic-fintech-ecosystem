from __future__ import annotations

import pytest

from afe_vector_memory import (
    AsyncMemoryAdapter,
    InMemoryStore,
    MemoryKind,
    MemoryUnavailableError,
    MemoryValidationError,
    Outcome,
)
from tests.helpers import NOW_MS, Clock, make_record


def _adapter(
    store: InMemoryStore | None = None, **kwargs: object
) -> tuple[AsyncMemoryAdapter, InMemoryStore]:
    store = store or InMemoryStore(clock=Clock())
    return AsyncMemoryAdapter(store, **kwargs), store  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_write_debate_then_recall_round_trip() -> None:
    adapter, store = _adapter()
    await adapter.write_debate(
        signal_id="sig-9", symbol="AAPL", regime="CRISIS", ts_ms=NOW_MS,
        outcome="abstain", text="Judge abstained: omega below threshold in crisis",
    )
    (view,) = await adapter.recall(symbol="MSFT", regime="CRISIS", query_text="omega abstained")
    assert view.kind == "debate_outcome"
    assert view.outcome == "abstain"
    assert view.symbol == "AAPL"  # regime-scoped, not symbol-scoped
    assert view.distance < 1.0
    assert store.count() == 1


@pytest.mark.asyncio
async def test_write_reflection_uses_its_own_id_namespace() -> None:
    adapter, store = _adapter()
    common = {"signal_id": "sig-1", "symbol": "AAPL", "regime": "CRISIS", "ts_ms": NOW_MS}
    await adapter.write_debate(outcome="pending", text="debate text", **common)
    await adapter.write_reflection(outcome="loss", text="reflection text", **common)
    assert store.count() == 2
    kinds = {h.record.kind for h in store.query("text", n_results=5)}
    assert kinds == {MemoryKind.DEBATE_OUTCOME, MemoryKind.POST_TRADE_REFLECTION}


@pytest.mark.asyncio
async def test_recall_empty_means_no_precedent_but_outage_raises() -> None:
    adapter, store = _adapter()
    assert await adapter.recall(symbol="AAPL", regime="CRISIS", query_text="anything") == []
    store.fail_with = ConnectionError("chroma down")
    with pytest.raises(MemoryUnavailableError):
        await adapter.recall(symbol="AAPL", regime="CRISIS", query_text="anything")


@pytest.mark.asyncio
async def test_max_distance_drops_weak_matches() -> None:
    adapter, store = _adapter(max_distance=0.05)
    store.add(make_record("far", text="completely unrelated words here"))
    assert await adapter.recall(symbol="AAPL", regime="TRENDING_BULL", query_text="breakout") == []


@pytest.mark.asyncio
async def test_limit_overrides_top_k() -> None:
    adapter, store = _adapter(top_k=1)
    for i in range(3):
        store.add(make_record(f"r{i}", text=f"breakout failed {i}"))
    query = {"symbol": "AAPL", "regime": "TRENDING_BULL", "query_text": "breakout"}
    assert len(await adapter.recall(**query)) == 1
    hits = await adapter.recall(**query, limit=3)
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_invalid_write_is_rejected() -> None:
    adapter, _ = _adapter()
    with pytest.raises(MemoryValidationError):
        await adapter.write_debate(
            signal_id="bad id", symbol="AAPL", regime="CRISIS", ts_ms=1, outcome="loss", text="t"
        )
    with pytest.raises(ValueError, match="nonsense"):
        await adapter.write_debate(
            signal_id="ok", symbol="AAPL", regime="CRISIS", ts_ms=1, outcome="nonsense", text="t"
        )


def test_top_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="top_k"):
        AsyncMemoryAdapter(InMemoryStore(), top_k=0)


def test_outcome_values_are_the_documented_set() -> None:
    assert {o.value for o in Outcome} == {
        "pending", "abstain", "win", "loss", "breakeven", "unknown",
    }


@pytest.mark.asyncio
async def test_hung_store_call_times_out_as_unavailable_not_empty() -> None:
    import threading

    release = threading.Event()

    class Hanging(InMemoryStore):
        def query(self, *args: object, **kwargs: object) -> list:  # type: ignore[override]
            release.wait(5)
            return []

    adapter = AsyncMemoryAdapter(Hanging(), timeout_s=0.05)
    try:
        with pytest.raises(MemoryUnavailableError, match="exceeded"):
            await adapter.recall(symbol="AAPL", regime="CRISIS", query_text="x")
    finally:
        release.set()


def test_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        AsyncMemoryAdapter(InMemoryStore(), timeout_s=0)
