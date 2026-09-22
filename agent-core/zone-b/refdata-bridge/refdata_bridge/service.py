"""Wires the Redis subscribers, the batch accumulator, and the Aegis push loop.

Fail-closed contract (see module docstrings of ``mapping``, ``redis_source``,
``aegis_client`` for the detail of each piece):
* a malformed/incomplete Redis message is logged and dropped, never guessed;
* a snapshot this bridge is not confident is fresh is pushed with
  ``is_stale=True``, never silently as fresh;
* an Aegis rejection is logged with its reason and that item's watermark is
  NOT advanced, so nothing "succeeds" silently;
* an unreachable Aegis raises inside one push cycle; the loop logs it, marks
  the health state unhealthy, and tries again next cycle -- it never crashes.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import structlog

from refdata_bridge.aegis_client import AegisRefdataClient, PushError
from refdata_bridge.batch import BatchState
from refdata_bridge.health import HealthState, touch_heartbeat
from refdata_bridge.mapping import MappingError, parse_regime, parse_snapshot
from refdata_bridge.redis_source import ClientFactory, run_subscriber

log = structlog.get_logger(__name__)

_NS_PER_S = 1_000_000_000


class BridgeService:
    def __init__(
        self,
        *,
        state: BatchState,
        health: HealthState,
        client: AegisRefdataClient,
        snapshot_stale_after_ns: int,
        max_batch_snapshots: int,
        batch_interval_s: float,
        heartbeat_path: Path | None = None,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        self._state = state
        self._health = health
        self._client = client
        self._snapshot_stale_after_ns = snapshot_stale_after_ns
        self._max_batch_snapshots = max_batch_snapshots
        self._batch_interval_s = batch_interval_s
        self._heartbeat_path = heartbeat_path
        self._now_ns = now_ns or (lambda: time.time_ns())

    async def on_snapshot_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
            snapshot = parse_snapshot(
                payload, now_ns=self._now_ns(), stale_after_ns=self._snapshot_stale_after_ns
            )
        except (json.JSONDecodeError, MappingError) as exc:
            log.warning("snapshot_message_rejected", error=str(exc))
            return
        self._state.record_snapshot(snapshot)

    async def on_regime_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
            regime = parse_regime(payload)
        except (json.JSONDecodeError, MappingError) as exc:
            log.warning("regime_message_rejected", error=str(exc))
            return
        self._state.record_regime(regime)

    async def run_push_loop(self, shutdown: asyncio.Event) -> None:
        while not shutdown.is_set():
            await self._push_once()
            if self._heartbeat_path is not None:
                touch_heartbeat(self._heartbeat_path)
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=self._batch_interval_s)
            except TimeoutError:
                continue

    async def _push_once(self) -> None:
        batch = self._state.build_batch(self._max_batch_snapshots)
        if not batch.snapshots and batch.regime is None:
            return
        now_ns = self._now_ns()
        try:
            result = await self._client.push(batch)
        except PushError as exc:
            log.error(
                "aegis_push_failed",
                error=str(exc),
                snapshots=len(batch.snapshots),
                had_regime=batch.regime is not None,
            )
            self._health.record_failure(now_ns, str(exc))
            return
        self._state.mark_applied(
            result.applied_snapshot_symbols,
            result.regime_applied,
            batch.regime.timestamp_ns if batch.regime is not None else None,
        )
        self._health.record_success(now_ns)
        log.info(
            "aegis_push_ok",
            applied=len(result.applied_snapshot_symbols),
            regime_applied=result.regime_applied,
            rejected=len(result.rejected),
        )


async def run_forever(
    *,
    service: BridgeService,
    snapshot_channel: str,
    regime_channel: str,
    snapshot_client_factory: ClientFactory,
    regime_client_factory: ClientFactory,
    shutdown: asyncio.Event,
) -> None:
    """Runs the two subscribers and the push loop concurrently until ``shutdown``."""
    await asyncio.gather(
        run_subscriber(
            snapshot_channel,
            service.on_snapshot_message,
            shutdown,
            client_factory=snapshot_client_factory,
        ),
        run_subscriber(
            regime_channel,
            service.on_regime_message,
            shutdown,
            client_factory=regime_client_factory,
        ),
        service.run_push_loop(shutdown),
    )
