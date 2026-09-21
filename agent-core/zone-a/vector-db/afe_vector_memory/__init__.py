"""AFE vector memory: debate outcomes and post-trade reflections over Chroma (ALI-39).

Fail-closed by design: an unreachable store raises `MemoryUnavailableError`; an empty list
from `query` always means "store answered, nothing matched".
"""

from __future__ import annotations

from .adapters import AsyncMemoryAdapter, PrecedentView
from .chroma_store import ChromaStore, open_chroma_store
from .config import VectorMemorySettings
from .embedding import Embedder, HashingEmbedder
from .errors import (
    EmbeddingError,
    MemoryConfigError,
    MemoryUnavailableError,
    MemoryValidationError,
    VectorMemoryError,
)
from .fake import InMemoryStore
from .models import MemoryHit, MemoryKind, MemoryRecord, Outcome, RetentionPolicy
from .store import MemoryStore

__all__ = [
    "AsyncMemoryAdapter",
    "ChromaStore",
    "Embedder",
    "EmbeddingError",
    "HashingEmbedder",
    "InMemoryStore",
    "MemoryConfigError",
    "MemoryHit",
    "MemoryKind",
    "MemoryRecord",
    "MemoryStore",
    "MemoryUnavailableError",
    "MemoryValidationError",
    "Outcome",
    "PrecedentView",
    "RetentionPolicy",
    "VectorMemoryError",
    "VectorMemorySettings",
    "open_chroma_store",
]
