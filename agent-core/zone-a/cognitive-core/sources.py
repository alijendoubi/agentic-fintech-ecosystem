"""Context sources: where raw snapshot messages come from.

* `RedisSnapshotSource`: pub/sub on `sensory:snapshots` (the sensory-array's channel).
  Any Redis failure propagates, which the runner treats as fatal (`SourceError`) so the
  container restarts rather than trading on a silently dead feed.
* `StaticSource`: a fixed list of messages, for tests and replays.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any


class StaticSource:
    def __init__(self, messages: Iterable[str | bytes]) -> None:
        self._messages = list(messages)

    async def messages(self) -> AsyncIterator[str | bytes]:
        for message in self._messages:
            yield message


class RedisSnapshotSource:
    def __init__(
        self,
        url: str,
        channel: str,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self._url = url
        self._channel = channel
        self._client_factory = client_factory or _default_client_factory

    async def messages(self) -> AsyncIterator[str | bytes]:
        client = self._client_factory(self._url)
        pubsub = client.pubsub()
        await pubsub.subscribe(self._channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    yield message["data"]
        finally:
            await pubsub.aclose()
            await client.aclose()


def _default_client_factory(url: str) -> Any:  # pragma: no cover - needs the redis package
    import redis.asyncio as redis

    return redis.Redis.from_url(url)
