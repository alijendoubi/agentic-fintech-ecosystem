"""Redis publisher for ``regime:labels``.

Payload (consumed by the Rust sensory-array, which ignores unknown keys)::

    {"symbol": "AAPL", "label": "TRENDING_BULL", "confidence": 0.87, "ts_ns": 1234}

``reason`` is added for ``REGIME_UNKNOWN`` messages so operators can see why the
detector abstained. The Redis client is injected, so tests use a fake.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Protocol

import structlog
from redis.exceptions import RedisError

from regime_detector.config import is_valid_symbol
from regime_detector.labels import RegimeLabel

log = structlog.get_logger()


class RedisPublisherClient(Protocol):
    """The slice of ``redis.asyncio.Redis`` this service needs."""

    async def publish(self, channel: str, message: str) -> int: ...


@dataclass(frozen=True, slots=True)
class RegimeMessage:
    """A validated outgoing message; invalid content cannot be constructed."""

    symbol: str
    label: RegimeLabel
    confidence: float
    ts_ns: int
    reason: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_symbol(self.symbol):
            raise ValueError(f"invalid symbol {self.symbol!r}")
        if not isinstance(self.label, RegimeLabel):
            raise ValueError(f"label {self.label!r} is not a RegimeLabel")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence {self.confidence!r} outside [0, 1]")

    def to_json(self) -> str:
        body: dict[str, object] = {
            "symbol": self.symbol,
            "label": self.label.value,
            "confidence": round(self.confidence, 4),
            "ts_ns": self.ts_ns,
        }
        if self.reason is not None and self.label is RegimeLabel.UNKNOWN:
            body["reason"] = self.reason
        return json.dumps(body, separators=(",", ":"))


class RegimePublisher:
    """Publishes messages; failures are logged and reported, never raised."""

    def __init__(self, client: RedisPublisherClient, channel: str) -> None:
        self._client = client
        self._channel = channel

    async def publish(self, message: RegimeMessage) -> bool:
        """True if Redis accepted the message. The next cycle republishes anyway."""
        try:
            await self._client.publish(self._channel, message.to_json())
        except (RedisError, OSError, TimeoutError, asyncio.TimeoutError) as exc:
            log.error(
                "regime_publish_failed",
                symbol=message.symbol,
                channel=self._channel,
                error=str(exc),
            )
            return False
        return True
