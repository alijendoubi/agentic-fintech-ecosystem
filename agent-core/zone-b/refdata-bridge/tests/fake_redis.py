"""In-memory fake for the slice of ``redis.asyncio`` pub/sub this bridge uses.

No ``fakeredis`` dependency exists anywhere in this repo yet, so a small,
purpose-built stub is used instead (per the task's fallback instruction).
"""

from __future__ import annotations

import asyncio
from typing import Any


class FakePubSub:
    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue
        self._subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self._subscribed.append(channel)

    async def get_message(
        self, ignore_subscribe_messages: bool = True, timeout: float = 1.0
    ) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def close(self) -> None:
        self.closed = True


class FakeRedisClient:
    """One shared queue feeds every ``pubsub()`` call (single-channel-per-test use)."""

    def __init__(self, *, fail_connect: bool = False) -> None:
        self.fail_connect = fail_connect
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.pubsub_instances: list[FakePubSub] = []

    def pubsub(self) -> FakePubSub:
        if self.fail_connect:
            raise ConnectionError("fake redis unreachable")
        ps = FakePubSub(self.queue)
        self.pubsub_instances.append(ps)
        return ps

    async def publish(self, channel: str, data: str) -> None:
        await self.queue.put({"type": "message", "channel": channel, "data": data})

    async def close(self) -> None:
        self.closed = True
