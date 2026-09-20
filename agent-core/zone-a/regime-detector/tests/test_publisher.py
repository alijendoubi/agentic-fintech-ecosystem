"""Redis publishing, with a fake client injected (no real Redis needed)."""

from __future__ import annotations

import json

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from regime_detector.labels import RegimeLabel
from regime_detector.publisher import RegimeMessage, RegimePublisher


class FakeRedis:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str) -> int:
        if self.error is not None:
            raise self.error
        self.published.append((channel, message))
        return 1


def _msg(label: RegimeLabel = RegimeLabel.TRENDING_BULL, **kw: object) -> RegimeMessage:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "label": label,
        "confidence": 0.87,
        "ts_ns": 1_700_000_000_000_000_000,
    }
    return RegimeMessage(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_regime_label_published() -> None:
    """Spec anchor: mock Redis, assert publish called with valid JSON."""
    redis = FakeRedis()
    ok = await RegimePublisher(redis, "regime:labels").publish(_msg())
    assert ok
    assert len(redis.published) == 1
    channel, raw = redis.published[0]
    assert channel == "regime:labels"
    body = json.loads(raw)
    assert body == {
        "symbol": "AAPL",
        "label": "TRENDING_BULL",
        "confidence": 0.87,
        "ts_ns": 1_700_000_000_000_000_000,
    }


@pytest.mark.asyncio
async def test_unknown_label_is_the_proto_name_and_carries_reason() -> None:
    redis = FakeRedis()
    unknown = _msg(RegimeLabel.UNKNOWN, confidence=0.0, reason="stale_data")
    await RegimePublisher(redis, "regime:labels").publish(unknown)
    body = json.loads(redis.published[0][1])
    assert body["label"] == "REGIME_UNKNOWN"
    assert body["reason"] == "stale_data"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RedisConnectionError("down"), OSError("reset"), TimeoutError()])
async def test_publish_errors_are_caught_and_reported(error: Exception) -> None:
    publisher = RegimePublisher(FakeRedis(error), "regime:labels")
    assert await publisher.publish(_msg()) is False


@pytest.mark.asyncio
async def test_publish_recovers_on_next_call() -> None:
    redis = FakeRedis(RedisConnectionError("down"))
    publisher = RegimePublisher(redis, "regime:labels")
    assert not await publisher.publish(_msg())
    redis.error = None
    assert await publisher.publish(_msg())
    assert len(redis.published) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"symbol": "aapl"},
        {"symbol": "A'; DROP"},
        {"confidence": float("nan")},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"label": "TRENDING_BULL"},  # plain str, not a RegimeLabel
    ],
)
def test_invalid_messages_cannot_be_constructed(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _msg(**kwargs)


def test_reason_only_emitted_for_unknown() -> None:
    body = json.loads(_msg(RegimeLabel.CRISIS, reason="ignored").to_json())
    assert "reason" not in body
