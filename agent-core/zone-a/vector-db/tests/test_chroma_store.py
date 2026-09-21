"""ChromaStore failure handling: an outage must never look like "no precedent"."""

from __future__ import annotations

from typing import Any

import pytest

from afe_vector_memory import (
    ChromaStore,
    EmbeddingError,
    HashingEmbedder,
    MemoryKind,
    MemoryUnavailableError,
    VectorMemorySettings,
    open_chroma_store,
)
from afe_vector_memory.chroma_store import (
    CLEANUP_MAX_BATCHES,
    COLLECTION_CONFIG,
    build_where,
    run_with_deadline,
)
from tests.fake_chroma import FakeClient, FakeCollection
from tests.helpers import DAY_MS, NOW_MS, Clock, make_record


def _store(collection: FakeCollection | None = None) -> tuple[ChromaStore, FakeCollection]:
    collection = collection or FakeCollection()
    return ChromaStore(collection, HashingEmbedder(), clock=Clock()), collection


def test_build_where_single_and_combined_clauses() -> None:
    assert build_where(now_ms=5) == {"expires_ms": {"$gt": 5}}
    combined = build_where(
        now_ms=5, regime="CRISIS", symbol="AAPL", kind=MemoryKind.POST_TRADE_REFLECTION
    )
    assert combined == {
        "$and": [
            {"expires_ms": {"$gt": 5}},
            {"regime": {"$eq": "CRISIS"}},
            {"symbol": {"$eq": "AAPL"}},
            {"kind": {"$eq": "post_trade_reflection"}},
        ]
    }


def test_add_sends_precomputed_embedding_and_flat_metadata() -> None:
    store, collection = _store()
    store.add(make_record())
    name, kwargs = collection.calls[-1]
    assert name == "upsert"
    assert len(kwargs["embeddings"][0]) == 256  # Chroma never embeds by itself
    assert kwargs["metadatas"][0] == {
        "kind": "debate_outcome", "symbol": "AAPL", "regime": "TRENDING_BULL", "ts_ms": NOW_MS,
        "expires_ms": NOW_MS + 90 * DAY_MS, "outcome": "loss", "signal_id": "sig-1",
    }


@pytest.mark.parametrize("operation", ["add", "query", "cleanup", "count", "ping"])
def test_server_errors_become_memory_unavailable(operation: str) -> None:
    store, collection = _store()
    collection.fail = ConnectionError("connection refused")
    actions: dict[str, Any] = {
        "add": lambda: store.add(make_record()),
        "query": lambda: store.query("breakout"),
        "cleanup": store.cleanup_expired,
        "count": store.count,
        "ping": store.ping,
    }
    with pytest.raises(MemoryUnavailableError, match="ConnectionError"):
        actions[operation]()


def test_outage_is_never_reported_as_an_empty_result() -> None:
    store, collection = _store()
    store.add(make_record())
    collection.fail = TimeoutError("read timed out")
    with pytest.raises(MemoryUnavailableError):
        store.query("breakout")


@pytest.mark.parametrize(
    "reply",
    [
        {},  # no keys at all
        {"ids": [["a"]], "documents": [["d"]], "metadatas": [[{}]]},  # missing distances
        {"ids": [["a"]], "documents": [[]], "metadatas": [[{}]], "distances": [[0.1]]},  # ragged
        {"ids": [["a"]], "documents": [["d"]], "metadatas": [[None]], "distances": [[0.1]]},
        {"ids": [["a"]], "documents": [["d"]], "metadatas": [[{"kind": "x"}]],
         "distances": [[0.1]]},
        {"ids": [["a"]], "documents": [["d"]], "metadatas": [[{}]], "distances": [["nan?"]]},
        {"ids": None, "documents": [["d"]], "metadatas": [[{}]], "distances": [[0.1]]},
    ],
)
def test_malformed_query_replies_are_errors_not_empty(reply: dict[str, Any]) -> None:
    store, collection = _store()
    collection.raw_query_result = reply
    with pytest.raises(MemoryUnavailableError):
        store.query("breakout")


def test_null_rows_from_chroma_mean_no_hits_only_when_ids_is_a_list() -> None:
    store, collection = _store()
    collection.raw_query_result = {
        "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]],
    }
    assert store.query("breakout") == []


def test_non_integer_count_is_rejected() -> None:
    store, collection = _store()
    collection.count = lambda: "3"  # type: ignore[method-assign, assignment, return-value]
    with pytest.raises(MemoryUnavailableError, match="non-integer"):
        store.count()


def test_cleanup_without_ids_key_is_an_error() -> None:
    store, collection = _store()
    collection.get = lambda **_: {}  # type: ignore[method-assign, assignment]
    with pytest.raises(MemoryUnavailableError, match="ids"):
        store.cleanup_expired()


def test_cleanup_that_never_converges_raises() -> None:
    clock = Clock()
    collection = FakeCollection()
    collection.ignore_deletes = True
    store = ChromaStore(collection, HashingEmbedder(), clock=clock)
    store.add(make_record())
    clock.now_ms = NOW_MS + 91 * DAY_MS
    with pytest.raises(MemoryUnavailableError, match="did not converge"):
        store.cleanup_expired()
    assert sum(1 for name, _ in collection.calls if name == "get") == CLEANUP_MAX_BATCHES


def test_embedding_failure_is_not_reported_as_unavailable() -> None:
    class Broken:
        def embed(self, texts: Any) -> Any:
            raise EmbeddingError("no model")

    store = ChromaStore(FakeCollection(), Broken())
    with pytest.raises(EmbeddingError):
        store.query("breakout")


def test_open_chroma_store_checks_heartbeat_and_uses_cosine_without_embedding_function() -> None:
    client = FakeClient()
    store = open_chroma_store(VectorMemorySettings(collection="afe_test"), client=client)
    store.ping()
    assert client.created_with == {
        "name": "afe_test",
        "configuration": COLLECTION_CONFIG,
        "embedding_function": None,
    }


def test_open_chroma_store_fails_closed_when_server_is_down() -> None:
    client = FakeClient()
    client.heartbeat_error = ConnectionError("refused")
    with pytest.raises(MemoryUnavailableError, match="refused"):
        open_chroma_store(VectorMemorySettings(), client=client)


def test_open_chroma_store_builds_http_client_from_settings() -> None:
    """No server is contacted: an unroutable local port fails fast and closed."""
    settings = VectorMemorySettings(host="127.0.0.1", port=1, connect_timeout_s=10.0)
    with pytest.raises(MemoryUnavailableError):
        open_chroma_store(settings)


def test_run_with_deadline_returns_value_and_reraises_errors() -> None:
    assert run_with_deadline(lambda: 7, 1.0) == 7
    with pytest.raises(KeyError):
        run_with_deadline(lambda: {}["missing"], 1.0)


def test_run_with_deadline_abandons_a_hung_call() -> None:
    import threading

    release = threading.Event()
    with pytest.raises(MemoryUnavailableError, match="exceeded"):
        run_with_deadline(lambda: release.wait(5), 0.05)
    release.set()


def test_open_chroma_store_times_out_on_a_hung_server() -> None:
    import threading

    release = threading.Event()

    class Hung(FakeClient):
        def heartbeat(self) -> int:
            release.wait(5)
            return 1

    try:
        with pytest.raises(MemoryUnavailableError, match="exceeded"):
            open_chroma_store(VectorMemorySettings(connect_timeout_s=0.1), client=Hung())
    finally:
        release.set()
