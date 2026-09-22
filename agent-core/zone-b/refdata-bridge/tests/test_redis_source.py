"""``redis_source.py``: pub/sub subscribe loop against the fake Redis client."""

from __future__ import annotations

import asyncio

import pytest
from fake_redis import FakeRedisClient

from refdata_bridge.redis_source import run_subscriber

pytestmark = pytest.mark.asyncio


async def test_subscriber_delivers_published_messages() -> None:
    client = FakeRedisClient()
    received: list[str] = []
    shutdown = asyncio.Event()

    async def on_message(raw: str) -> None:
        received.append(raw)
        if len(received) == 2:
            shutdown.set()

    await client.publish("chan", "one")
    await client.publish("chan", "two")

    await asyncio.wait_for(
        run_subscriber("chan", on_message, shutdown, client_factory=lambda: client), timeout=5.0
    )
    assert received == ["one", "two"]
    assert client.pubsub_instances[0]._subscribed == ["chan"]
    assert client.closed is True


async def test_subscriber_reconnects_after_connect_failure() -> None:
    attempts = {"n": 0}
    good_client = FakeRedisClient()
    shutdown = asyncio.Event()
    received: list[str] = []

    def factory() -> FakeRedisClient:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeRedisClient(fail_connect=True)
        return good_client

    async def on_message(raw: str) -> None:
        received.append(raw)
        shutdown.set()

    async def publish_soon() -> None:
        await asyncio.sleep(0.6)
        await good_client.publish("chan", "hello")

    task = asyncio.create_task(publish_soon())
    await asyncio.wait_for(
        run_subscriber("chan", on_message, shutdown, client_factory=factory), timeout=10.0
    )
    await task
    assert received == ["hello"]
    assert attempts["n"] >= 2


async def test_subscriber_stops_cleanly_on_shutdown_with_no_messages() -> None:
    client = FakeRedisClient()
    shutdown = asyncio.Event()

    async def never_called(_raw: str) -> None:
        raise AssertionError("no messages were published")

    async def stop_soon() -> None:
        await asyncio.sleep(0.1)
        shutdown.set()

    task = asyncio.create_task(stop_soon())
    await asyncio.wait_for(
        run_subscriber("chan", never_called, shutdown, client_factory=lambda: client), timeout=5.0
    )
    await task
