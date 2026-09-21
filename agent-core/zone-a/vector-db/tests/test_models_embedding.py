from __future__ import annotations

import math
from collections.abc import Sequence

import pytest
from afe_vector_memory import (
    EmbeddingError,
    HashingEmbedder,
    MemoryHit,
    MemoryKind,
    MemoryRecord,
    MemoryValidationError,
    Outcome,
    RetentionPolicy,
)
from afe_vector_memory.embedding import embed_checked
from afe_vector_memory.models import MAX_TEXT_CHARS
from tests.helpers import DAY_MS, NOW_MS, make_record


def test_record_coerces_string_enums() -> None:
    record = MemoryRecord("r", "debate_outcome", "text", "AAPL", "CRISIS", 1, "win")  # type: ignore[arg-type]
    assert record.kind is MemoryKind.DEBATE_OUTCOME
    assert record.outcome is Outcome.WIN


@pytest.mark.parametrize(
    "overrides",
    [
        {"record_id": ""},
        {"record_id": "has space"},
        {"kind": "nonsense"},
        {"outcome": "nonsense"},
        {"text": "   "},
        {"text": "x" * (MAX_TEXT_CHARS + 1)},
        {"symbol": "aapl"},
        {"regime": "trending bull"},
        {"ts_ms": -1},
        {"ts_ms": True},
        {"ts_ms": 1.5},
        {"signal_id": "s" * 129},
    ],
)
def test_record_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(MemoryValidationError):
        make_record(**overrides)  # type: ignore[arg-type]


def test_hit_rejects_non_finite_distance() -> None:
    with pytest.raises(MemoryValidationError):
        MemoryHit(make_record(), float("nan"))


def test_retention_expiry_per_kind() -> None:
    policy = RetentionPolicy(debate_days=2, reflection_days=5)
    assert policy.expires_at_ms(MemoryKind.DEBATE_OUTCOME, NOW_MS) == NOW_MS + 2 * DAY_MS
    assert policy.expires_at_ms(MemoryKind.POST_TRADE_REFLECTION, NOW_MS) == NOW_MS + 5 * DAY_MS


@pytest.mark.parametrize("days", [0, -1, 3651, True, 1.5])
def test_retention_rejects_bad_days(days: object) -> None:
    with pytest.raises(MemoryValidationError):
        RetentionPolicy(debate_days=days)  # type: ignore[arg-type]


def test_hashing_embedder_is_deterministic_normalised_and_similarity_ordered() -> None:
    embedder = HashingEmbedder(64)
    a, b, c = embedder.embed(
        ["breakout failed on volume", "breakout failed on volume", "earnings gap up"]
    )
    assert a == b
    assert math.isclose(sum(x * x for x in a), 1.0)
    assert embedder.dimension == 64
    near = embedder.embed(["breakout failed on low volume"])[0]
    dot = sum(x * y for x, y in zip(a, near, strict=True))
    dot_far = sum(x * y for x, y in zip(a, c, strict=True))
    assert dot > dot_far


@pytest.mark.parametrize("text", ["", "!!! ???"])
def test_hashing_embedder_rejects_untokenisable_text(text: str) -> None:
    with pytest.raises(EmbeddingError):
        HashingEmbedder().embed([text])


@pytest.mark.parametrize("dimension", [0, 7, True, 3.5])
def test_hashing_embedder_rejects_bad_dimension(dimension: object) -> None:
    with pytest.raises(ValueError, match="dimension"):
        HashingEmbedder(dimension)  # type: ignore[arg-type]


class _Stub:
    def __init__(self, result: object = None, error: Exception | None = None) -> None:
        self._result, self._error = result, error

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self._error:
            raise self._error
        return self._result  # type: ignore[return-value]


@pytest.mark.parametrize(
    "result",
    [
        [],  # wrong count
        [[]],  # empty vector
        [[float("nan"), 1.0]],
        [[float("inf"), 1.0]],
        [[0.0, 0.0]],
        [[1.0, 0.0], [1.0]],  # ragged (two texts)
    ],
)
def test_embed_checked_rejects_unusable_vectors(result: list[list[float]]) -> None:
    with pytest.raises(EmbeddingError):
        embed_checked(_Stub(result), ["one", "two"][: max(1, len(result))])


def test_embed_checked_wraps_foreign_errors_and_passes_ours_through() -> None:
    with pytest.raises(EmbeddingError, match="RuntimeError"):
        embed_checked(_Stub(error=RuntimeError("model gone")), ["x"])
    with pytest.raises(EmbeddingError, match="mine"):
        embed_checked(_Stub(error=EmbeddingError("mine")), ["x"])
    assert embed_checked(_Stub([[1, 2]]), ["x"]) == [[1.0, 2.0]]
