from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from afe_vector_memory import (
    ChromaStore,
    HashingEmbedder,
    InMemoryStore,
    MemoryStore,
    RetentionPolicy,
)
from tests.fake_chroma import FakeCollection
from tests.helpers import Clock

StoreFactory = Callable[[Clock], MemoryStore]


def _fake_store(clock: Clock) -> MemoryStore:
    return InMemoryStore(HashingEmbedder(), RetentionPolicy(10, 30), clock)


def _chroma_over_fake(clock: Clock) -> MemoryStore:
    return ChromaStore(FakeCollection(), HashingEmbedder(), RetentionPolicy(10, 30), clock)


def _chroma_real(clock: Clock) -> MemoryStore:
    """Real Chroma engine, in-process (no server, no network). Skipped if not installed."""
    chromadb: Any = pytest.importorskip("chromadb")
    try:
        client = chromadb.EphemeralClient()
    except RuntimeError as exc:  # chromadb-client (http-only) is installed, not the engine
        pytest.skip(f"in-process Chroma engine unavailable: {exc}")
    collection = client.get_or_create_collection(
        f"t{uuid.uuid4().hex}",
        configuration={"hnsw": {"space": "cosine"}},
        embedding_function=None,
    )
    return ChromaStore(collection, HashingEmbedder(), RetentionPolicy(10, 30), clock)


@pytest.fixture(params=["in_memory", "chroma_fake", "chroma_real"])
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    """Every MemoryStore implementation must satisfy the same contract tests."""
    factories: dict[str, StoreFactory] = {
        "in_memory": _fake_store,
        "chroma_fake": _chroma_over_fake,
        "chroma_real": _chroma_real,
    }
    return factories[request.param]
