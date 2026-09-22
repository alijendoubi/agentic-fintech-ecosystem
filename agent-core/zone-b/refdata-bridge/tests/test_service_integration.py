"""End-to-end: fake Redis pub/sub -> BridgeService -> real mTLS Aegis (fake_aegis.py).

Proves the three end-to-end fail-closed behaviours the task requires:
* a valid snapshot+regime batch is pushed and applied;
* a stale/malformed snapshot is marked ``is_stale`` or skipped, never sent as
  fresh;
* an unreachable Aegis is retried/logged next cycle, not a crash.
"""

from __future__ import annotations

import asyncio
import json

import aegis_pb2
import aegis_pb2_grpc
import grpc
import pytest
from fake_aegis import FakeAegisServicer, generate_mtls_material, start_fake_aegis_server

from refdata_bridge.aegis_client import AegisRefdataClient, PushError
from refdata_bridge.batch import BatchState, PendingBatch
from refdata_bridge.health import HealthState
from refdata_bridge.mapping import ReferenceSnapshotData
from refdata_bridge.service import BridgeService

pytestmark = pytest.mark.asyncio

NOW_NS = 1_700_000_000_000_000_000


def snapshot_json(**overrides: object) -> str:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "ingestion_ts_ns": NOW_NS,
        "mid_price": 150.5,
        "adv_30d": 2_000_000.0,
        "is_stale": False,
        "warmup": False,
    }
    return json.dumps({**base, **overrides})


def regime_json(**overrides: object) -> str:
    base: dict[str, object] = {
        "symbol": "AAPL",
        "label": "TRENDING_BULL",
        "confidence": 0.9,
        "ts_ns": NOW_NS,
    }
    return json.dumps({**base, **overrides})


@pytest.fixture
def mtls_server():
    material = generate_mtls_material()
    servicer = FakeAegisServicer()
    server, port = start_fake_aegis_server(servicer, material)
    yield material, servicer, port
    server.stop(None)


def _channel(material, port: int) -> grpc.aio.Channel:
    creds = grpc.ssl_channel_credentials(
        root_certificates=material.ca_cert_pem,
        certificate_chain=material.client_cert_pem,
        private_key=material.client_key_pem,
    )
    return grpc.aio.secure_channel(f"localhost:{port}", creds)


def _service(client: AegisRefdataClient) -> tuple[BridgeService, BatchState, HealthState]:
    state = BatchState()
    health = HealthState(clock=lambda: NOW_NS / 1e9)
    service = BridgeService(
        state=state,
        health=health,
        client=client,
        snapshot_stale_after_ns=2_000_000_000,
        max_batch_snapshots=64,
        batch_interval_s=0.05,
        now_ns=lambda: NOW_NS,
    )
    return service, state, health


async def test_valid_batch_end_to_end_is_applied(mtls_server) -> None:
    material, servicer, port = mtls_server
    channel = _channel(material, port)
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=5.0)
        service, state, health = _service(client)

        await service.on_snapshot_message(snapshot_json())
        await service.on_regime_message(regime_json())
        await service._push_once()

        assert servicer.applied[0].symbol == "AAPL"
        assert servicer.applied[0].is_stale is False
        assert len(servicer.applied_regimes) == 1
        assert health.snapshot()["healthy"] is True
    finally:
        await channel.close()


async def test_stale_snapshot_is_pushed_marked_stale_not_as_fresh(mtls_server) -> None:
    material, servicer, port = mtls_server
    channel = _channel(material, port)
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=5.0)
        service, state, health = _service(client)

        # warmup=True -> "not confident yet", must never be forwarded as fresh.
        await service.on_snapshot_message(snapshot_json(warmup=True))
        await service._push_once()

        assert len(servicer.applied) == 1
        assert servicer.applied[0].is_stale is True
    finally:
        await channel.close()


async def test_malformed_snapshot_is_dropped_not_sent(mtls_server) -> None:
    material, servicer, port = mtls_server
    channel = _channel(material, port)
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=5.0)
        service, state, health = _service(client)

        await service.on_snapshot_message(snapshot_json(mid_price=None))  # missing price
        await service.on_snapshot_message("not even json")
        await service._push_once()

        assert servicer.applied == []
        assert state.known_symbols() == 0
    finally:
        await channel.close()


async def test_unreachable_aegis_is_logged_and_retried_not_crashed() -> None:
    channel = grpc.aio.insecure_channel("127.0.0.1:1")
    try:
        stub = aegis_pb2_grpc.AegisStub(channel)
        client = AegisRefdataClient(stub=stub, aegis_pb2=aegis_pb2, timeout_s=0.5)
        service, state, health = _service(client)

        await service.on_snapshot_message(snapshot_json())
        # _push_once must swallow PushError, not raise, and mark health unhealthy.
        await service._push_once()

        assert health.snapshot()["last_error"] is not None
        assert health.snapshot()["healthy"] is False

        # The item was never applied, so it must still be due next cycle
        # (watermark not advanced) -- proving nothing was silently dropped.
        batch: PendingBatch = state.build_batch(max_snapshots=10)
        assert len(batch.snapshots) == 1
    finally:
        await channel.close()


async def test_run_push_loop_survives_repeated_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """The background loop itself must not die when every push raises."""

    class AlwaysFailingClient:
        calls = 0

        async def push(self, _batch: PendingBatch) -> None:
            AlwaysFailingClient.calls += 1
            raise PushError("boom")

    state = BatchState()
    state.record_snapshot(
        ReferenceSnapshotData(
            symbol="AAPL",
            mid_price_nanos=1,
            adv_30d_nanos=1,
            as_of_ns=NOW_NS,
            is_stale=False,
        )
    )
    health = HealthState(clock=lambda: NOW_NS / 1e9)
    service = BridgeService(
        state=state,
        health=health,
        client=AlwaysFailingClient(),
        snapshot_stale_after_ns=2_000_000_000,
        max_batch_snapshots=64,
        batch_interval_s=0.02,
        now_ns=lambda: NOW_NS,
    )
    shutdown = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.08)
        shutdown.set()

    await asyncio.gather(service.run_push_loop(shutdown), stop_soon())
    assert AlwaysFailingClient.calls >= 2, "loop must keep retrying, not die on first failure"
