"""The MemoryStore contract, run against every implementation."""

from __future__ import annotations

import pytest
from afe_vector_memory import MemoryKind, MemoryValidationError, Outcome
from tests.conftest import StoreFactory
from tests.helpers import DAY_MS, NOW_MS, Clock, make_record


def test_add_then_query_returns_the_record(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    store.add(make_record())
    (hit,) = store.query("breakout failed reversed", n_results=3)
    assert hit.record == make_record()
    assert 0.0 <= hit.distance < 1.0
    assert store.count() == 1


def test_query_on_empty_store_is_an_empty_list_not_an_error(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    store.ping()
    assert store.query("anything at all") == []


def test_results_are_ordered_by_similarity(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    store.add(make_record("far", text="earnings gap up on guidance raise"))
    store.add(make_record("near", text="breakout above resistance failed reversed volume"))
    hits = store.query("breakout above resistance failed", n_results=5)
    assert [h.record.record_id for h in hits] == ["near", "far"]
    assert hits[0].distance < hits[1].distance


def test_regime_filter_excludes_other_regimes(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    store.add(make_record("bull", regime="TRENDING_BULL"))
    store.add(make_record("crisis", regime="CRISIS"))
    hits = store.query("breakout above resistance", regime="CRISIS")
    assert [h.record.record_id for h in hits] == ["crisis"]
    assert store.query("breakout above resistance", regime="LOW_VOL_CHOP") == []


def test_symbol_and_kind_filters_combine_with_regime(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    store.add(make_record("a", symbol="AAPL"))
    store.add(make_record("b", symbol="MSFT"))
    store.add(make_record("c", symbol="AAPL", kind=MemoryKind.POST_TRADE_REFLECTION))
    hits = store.query(
        "breakout above resistance", regime="TRENDING_BULL", symbol="AAPL",
        kind=MemoryKind.POST_TRADE_REFLECTION,
    )
    assert [h.record.record_id for h in hits] == ["c"]


def test_n_results_limits_output(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    for i in range(5):
        store.add(make_record(f"r{i}", text=f"breakout number {i} failed"))
    assert len(store.query("breakout failed", n_results=2)) == 2


def test_add_is_an_upsert_by_record_id(store_factory: StoreFactory) -> None:
    store = store_factory(Clock())
    store.add(make_record(outcome=Outcome.PENDING))
    store.add(make_record(outcome=Outcome.WIN))
    assert store.count() == 1
    assert store.query("breakout")[0].record.outcome == Outcome.WIN


def test_expired_records_are_hidden_even_before_cleanup(store_factory: StoreFactory) -> None:
    clock = Clock()
    store = store_factory(clock)
    store.add(make_record("deb", kind=MemoryKind.DEBATE_OUTCOME))  # retention 10 days
    store.add(make_record("ref", kind=MemoryKind.POST_TRADE_REFLECTION))  # retention 30 days
    clock.now_ms = NOW_MS + 11 * DAY_MS
    assert [h.record.record_id for h in store.query("breakout")] == ["ref"]
    assert store.count() == 2  # still stored until cleanup runs
    clock.now_ms = NOW_MS + 31 * DAY_MS
    assert store.query("breakout") == []


def test_cleanup_removes_only_expired_records(store_factory: StoreFactory) -> None:
    clock = Clock()
    store = store_factory(clock)
    store.add(make_record("deb", kind=MemoryKind.DEBATE_OUTCOME))
    store.add(make_record("ref", kind=MemoryKind.POST_TRADE_REFLECTION))
    assert store.cleanup_expired() == 0
    clock.now_ms = NOW_MS + 11 * DAY_MS
    assert store.cleanup_expired() == 1
    assert store.count() == 1
    assert store.cleanup_expired() == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"n_results": 0},
        {"n_results": 10_000},
        {"n_results": True},
        {"regime": "trending bull"},
        {"symbol": "aapl!"},
    ],
)
def test_invalid_query_arguments_are_rejected(
    store_factory: StoreFactory, kwargs: dict[str, object]
) -> None:
    store = store_factory(Clock())
    with pytest.raises(MemoryValidationError):
        store.query("breakout", **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("text", ["", "   "])
def test_blank_query_text_is_rejected(store_factory: StoreFactory, text: str) -> None:
    with pytest.raises(MemoryValidationError):
        store_factory(Clock()).query(text)


def test_add_rejects_non_records(store_factory: StoreFactory) -> None:
    with pytest.raises(MemoryValidationError):
        store_factory(Clock()).add("not a record")  # type: ignore[arg-type]
