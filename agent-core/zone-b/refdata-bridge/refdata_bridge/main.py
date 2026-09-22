"""Entrypoint: wires config, Redis, Aegis mTLS channel, health server, and runs forever."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import structlog

from refdata_bridge.aegis_client import (
    AegisRefdataClient,
    load_generated_protos,
    open_aegis_channel,
)
from refdata_bridge.batch import BatchState
from refdata_bridge.config import ConfigError, Settings
from refdata_bridge.health import HealthState, start_health_server
from refdata_bridge.redis_source import default_client_factory
from refdata_bridge.service import BridgeService, run_forever

log = structlog.get_logger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )


async def _amain(settings: Settings) -> None:
    protos = load_generated_protos(settings.proto_dir)
    channel = open_aegis_channel(settings)
    stub = protos["aegis_pb2_grpc"].AegisStub(channel)
    client = AegisRefdataClient(
        stub=stub,
        aegis_pb2=protos["aegis_pb2"],
        market_snapshot_pb2=protos["market_snapshot_pb2"],
        timeout_s=settings.aegis_rpc_timeout_s,
    )

    health = HealthState(max_silence_s=max(30.0, 3 * settings.batch_interval_s))
    start_health_server(settings.health_host, settings.health_port, health)

    service = BridgeService(
        state=BatchState(),
        health=health,
        client=client,
        snapshot_stale_after_ns=int(settings.snapshot_stale_after_s * 1_000_000_000),
        max_batch_snapshots=settings.max_batch_snapshots,
        batch_interval_s=settings.batch_interval_s,
        heartbeat_path=settings.heartbeat_path,
    )

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover - Windows dev fallback
            loop.add_signal_handler(sig, shutdown.set)

    try:
        await run_forever(
            service=service,
            snapshot_channel=settings.snapshot_channel,
            regime_channel=settings.regime_channel,
            snapshot_client_factory=default_client_factory(settings.redis_url),
            regime_client_factory=default_client_factory(settings.redis_url),
            shutdown=shutdown,
        )
    finally:
        await channel.close()


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"refdata-bridge: invalid configuration: {exc}", file=sys.stderr)
        return 2
    _configure_logging(settings.log_level)
    log.info(
        "refdata_bridge_starting",
        aegis_target=settings.aegis_target,
        environment=settings.environment,
        snapshot_channel=settings.snapshot_channel,
        regime_channel=settings.regime_channel,
    )
    asyncio.run(_amain(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
