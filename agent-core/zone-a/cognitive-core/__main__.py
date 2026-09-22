"""Composition root: `python -m cognitive_core` starts the runner service.

Exit codes: 0 clean stop (SIGTERM/SIGINT), 1 the context source died or ended, 2 bad
configuration or a missing required component. Both non-zero codes make the orchestrator
restart the container; nothing is emitted while a component is missing.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import Callable, Mapping
from typing import Any

import structlog

from .config import load_settings
from .graph import build_debate_graph
from .halt import HaltGate
from .health import HealthServer, HealthState
from .memory import PrecedentProvider, ReflectionWriter
from .runner_config import RunnerSettings
from .service import CognitiveRunner, ContextSource, SourceError
from .sinks import (
    AegisGrpcSink,
    AegisRelaySink,
    LogSink,
    MotorGrpcSink,
    MotorSink,
    SignalSink,
    load_generated_motor_protos,
    load_generated_protos,
    open_aegis_channel,
    open_motor_channel,
)
from .sources import RedisSnapshotSource

log = structlog.get_logger(__name__)

EXIT_OK = 0
EXIT_SOURCE = 1
EXIT_CONFIG = 2


def build_sink(settings: RunnerSettings, env: Mapping[str, str] | None = None) -> SignalSink:
    if settings.sink == "log":
        return LogSink()
    protos = load_generated_protos(settings.proto_dir)  # ImportError -> caller exits 2
    channel = open_aegis_channel(settings.aegis_host, settings.aegis_port, env)
    aegis_stub = protos["aegis_pb2_grpc"].AegisStub(channel)
    motor_sink = _build_motor_sink(settings, env)
    if motor_sink is None:
        # Known limitation: execution_motor.proto (PKG-X3) has not landed in this worktree,
        # so there is nothing to relay an approved AegisDecision to yet. Aegis is still
        # submitted to normally; see sinks.py module docstring / README "Known limitations".
        return AegisGrpcSink(
            stub=aegis_stub,
            trade_signal_pb2=protos["trade_signal_pb2"],
            strategy_id=settings.strategy_id,
            timeout_s=settings.aegis_timeout_s,
        )
    return AegisRelaySink(
        aegis_stub=aegis_stub,
        trade_signal_pb2=protos["trade_signal_pb2"],
        strategy_id=settings.strategy_id,
        motor_sink=motor_sink,
        aegis_timeout_s=settings.aegis_timeout_s,
    )


def _build_motor_sink(settings: RunnerSettings, env: Mapping[str, str] | None) -> MotorSink | None:
    """`None` means "not available yet" (see `load_generated_motor_protos`); the caller falls
    back to an Aegis-only sink rather than blocking startup on a package that does not exist.
    """
    try:
        motor_protos = load_generated_motor_protos(settings.proto_dir)
    except ImportError as exc:
        log.warning(
            "motor_relay_unavailable",
            reason=f"{type(exc).__name__}: {exc}",
            detail="execution_motor.proto not generated; Aegis approvals are not forwarded "
            "to execution-motor",
        )
        return None
    motor_channel = open_motor_channel(settings.motor_target, env)  # ConfigError -> caller exits 2
    motor_stub = motor_protos["execution_motor_pb2_grpc"].ExecutionMotorStub(motor_channel)
    return MotorGrpcSink(stub=motor_stub, timeout_s=settings.motor_timeout_s)


def build_memory(
    env: Mapping[str, str], settings: RunnerSettings
) -> tuple[PrecedentProvider | None, ReflectionWriter | None]:
    """Optional vector memory. Only imported when explicitly enabled."""
    if not settings.memory_enabled:
        return None, None
    from afe_vector_memory import (  # type: ignore[import-not-found]
        AsyncMemoryAdapter,
        VectorMemorySettings,
        open_chroma_store,
    )

    memory_settings = VectorMemorySettings.from_env(env)
    adapter = AsyncMemoryAdapter(
        open_chroma_store(memory_settings),
        top_k=settings.memory_top_k,
        timeout_s=memory_settings.timeout_s,
    )
    return adapter, adapter


async def serve(runner: CognitiveRunner, source: ContextSource, stop: asyncio.Event) -> int:
    try:
        await runner.run(source, stop)
    except SourceError as exc:
        log.error("source_failed", error=str(exc))
        return EXIT_SOURCE
    if not stop.is_set():
        log.error("source_ended_unexpectedly")
        return EXIT_SOURCE
    return EXIT_OK


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def amain(
    env: Mapping[str, str],
    *,
    graph_factory: Callable[..., Any] = build_debate_graph,
    source: ContextSource | None = None,
    stop: asyncio.Event | None = None,
) -> int:
    try:
        runner_settings = RunnerSettings.from_env(env)
        cognitive = load_settings(env)
        sink = build_sink(runner_settings, env)
        precedents, reflections = build_memory(env, runner_settings)
        graph = graph_factory(cognitive)
    except Exception as exc:  # noqa: BLE001 - any startup failure must exit non-zero, not trade
        log.error("startup_failed", error_type=type(exc).__name__, error=str(exc)[:300])
        return EXIT_CONFIG

    health = HealthState(runner_settings.max_beat_age_s)
    server = HealthServer(health, runner_settings.health_host, runner_settings.health_port)
    runner = CognitiveRunner(
        settings=runner_settings,
        cognitive=cognitive,
        graph=graph,
        sink=sink,
        halt=HaltGate(env),
        health=health,
        precedents=precedents,
        reflections=reflections,
    )
    stop_event = stop or asyncio.Event()
    if stop is None:
        _install_signal_handlers(stop_event)
    feed = source or RedisSnapshotSource(
        runner_settings.redis_url, runner_settings.snapshot_channel
    )
    server.start()
    log.info("cognitive_core_started", health_port=server.port, sink=runner_settings.sink)
    try:
        return await serve(runner, feed, stop_event)
    finally:
        server.stop()


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    del argv  # no CLI flags: configuration is environment-only
    return asyncio.run(amain(os.environ if env is None else env))


if __name__ == "__main__":
    sys.exit(main())
