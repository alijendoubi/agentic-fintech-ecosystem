"""Embedding contract plus a dependency-free, offline default.

Zone A has no outbound internet, so nothing here downloads a model. Production callers
may inject any `Embedder` (for example one backed by a Bedrock embedding endpoint);
tests inject `HashingEmbedder` or their own stub.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

from .errors import EmbeddingError

_TOKEN = re.compile(r"[a-z0-9_]+")
DEFAULT_DIMENSION = 256
MIN_DIMENSION = 8


class Embedder(Protocol):
    """Maps texts to equal-length, finite, non-zero float vectors."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Signed feature hashing of word unigrams and bigrams, L2-normalised.

    Deterministic and offline. It captures lexical overlap only (no semantics), which is
    honest and good enough for regime/symbol-filtered precedent lookup until a real
    embedding model is injected.
    """

    def __init__(self, dimension: int = DEFAULT_DIMENSION) -> None:
        valid = isinstance(dimension, int) and not isinstance(dimension, bool)
        if not valid or dimension < MIN_DIMENSION:
            raise ValueError(f"dimension must be an int >= {MIN_DIMENSION}")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @staticmethod
    def _features(text: str) -> list[str]:
        tokens = _TOKEN.findall(text.lower())
        return tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:], strict=False)]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        features = self._features(text)
        if not features:
            raise EmbeddingError("text has no embeddable tokens")
        vector = [0.0] * self._dimension
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dimension
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            raise EmbeddingError("text embeds to the zero vector")
        return [v / norm for v in vector]


def embed_checked(embedder: Embedder, texts: Sequence[str]) -> list[list[float]]:
    """Call the embedder and reject anything a vector index must not receive."""
    try:
        vectors = embedder.embed(texts)
    except EmbeddingError:
        raise
    except Exception as exc:  # noqa: BLE001 - injected code: surface as a typed error
        raise EmbeddingError(f"embedder failed: {type(exc).__name__}: {exc}") from exc
    if len(vectors) != len(texts):
        raise EmbeddingError(f"embedder returned {len(vectors)} vectors for {len(texts)} texts")
    dimension: int | None = None
    checked: list[list[float]] = []
    for vector in vectors:
        values = [float(x) for x in vector]
        if not values or any(not math.isfinite(x) for x in values):
            raise EmbeddingError("embedding is empty or contains non-finite values")
        if not any(values):
            raise EmbeddingError("embedding is the zero vector")
        if dimension is not None and len(values) != dimension:
            raise EmbeddingError("embedder returned vectors of differing dimensions")
        dimension = len(values)
        checked.append(values)
    return checked
