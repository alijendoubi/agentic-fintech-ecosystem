"""Redis pub/sub subscriber (matches the existing repo pattern: sensory-array PUBLISHes
``sensory:snapshots`` and regime-detector PUBLISHes ``regime:labels`` -- see
``agent-core/zone-b/sensory-array/src/publisher.rs`` and
``agent-core/zone-a/regime-detector/regime_detector/publisher.py``. This bridge
SUBSCRIBEs to both channels rather than polling a key, to match that pattern.

Fail-closed: a connection failure is logged and retried with jittered backoff; it
never raises out of ``run_subscriber`` and never silently stops (the caller's health
check will show a stalled bridge once no message updates ``on_message``).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

MIN_BACKOFF_S = 0.5
MAX_BACKOFF_S = 10.0


class PubSub(Protocol):
    async def subscribe(self, channel: str) -> Any: ...
    async def get_message(
        self, ignore_subscribe_messages: bool, timeout: float
    ) -> dict[str, Any] | None: ...
    async def close(self) -> None: ...


class RedisLike(Protocol):
    def pubsub(self) -> PubSub: ...
    async def close(self) -> None: ...


ClientFactory = Callable[[], RedisLike]
OnMessage = Callable[[str], Awaitable[None]]


def default_client_factory(redis_url: str) -> ClientFactory:
    """Real ``redis.asyncio`` client factory. Imported lazily so tests never need redis running."""

    def _connect() -> RedisLike:
        import redis.asyncio as redis_asyncio

        return redis_asyncio.Redis.from_url(redis_url)

    return _connect


def jittered_backoff(failures: int) -> float:
    """Exponential backoff with full jitter, capped, mirrors sensory-array's Rust helper."""
    ceiling = min(MAX_BACKOFF_S, MIN_BACKOFF_S * (2 ** max(0, failures - 1)))
    return random.uniform(MIN_BACKOFF_S, ceiling)  # noqa: S311 - jitter, not security


async def run_subscriber(
    channel: str,
    on_message: OnMessage,
    shutdown: asyncio.Event,
    *,
    client_factory: ClientFactory,
) -> None:
    """Subscribe to ``channel`` until ``shutdown`` is set, reconnecting on any failure."""
    failures = 0
    while not shutdown.is_set():
        try:
            await _subscribe_once(channel, on_message, shutdown, client_factory)
            failures = 0
        except Exception as exc:  # noqa: BLE001 - a subscriber must survive and retry
            failures += 1
            log.warning(
                "redis_subscriber_failed",
                channel=channel,
                failures=failures,
                error=f"{type(exc).__name__}: {exc}",
            )
        if shutdown.is_set():
            return
        delay = jittered_backoff(failures)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=delay)
        except TimeoutError:
            continue


async def _subscribe_once(
    channel: str,
    on_message: OnMessage,
    shutdown: asyncio.Event,
    client_factory: ClientFactory,
) -> None:
    client = client_factory()
    try:
        pubsub = client.pubsub()
        await pubsub.subscribe(channel)
        log.info("redis_subscriber_listening", channel=channel)
        try:
            while not shutdown.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is None:
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", errors="replace")
                if isinstance(data, str):
                    await on_message(data)
        finally:
            await pubsub.close()
    finally:
        await client.close()
