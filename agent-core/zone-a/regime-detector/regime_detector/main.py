"""Process wiring: settings, logging, Redis, signals. Entry point is ``run()``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import redis.asyncio as aioredis
import structlog
from regime_detector.config import ConfigError, Settings
from regime_detector.model_store import ModelStore
from regime_detector.publisher import RegimePublisher
from regime_detector.questdb_client import QuestDbClient
from regime_detector.service import RegimeService

log = structlog.get_logger()

EXIT_CONFIG_ERROR = 2
_REDIS_TIMEOUT_S = 2.0


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


def build_service(settings: Settings) -> RegimeService:
    client = aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=_REDIS_TIMEOUT_S,
        socket_connect_timeout=_REDIS_TIMEOUT_S,
    )
    store = ModelStore(settings.model_dir, settings.model_hmac_key)
    if not store.enabled:
        log.critical(
            "model_persistence_disabled",
            reason="MODEL_HMAC_KEY not set: models are trained in memory only, never loaded/saved",
        )
    return RegimeService(
        settings,
        QuestDbClient(settings),
        RegimePublisher(client, settings.redis_channel),
        store,
    )


async def amain(settings: Settings) -> int:
    service = build_service(settings)
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    log.info(
        "regime_detector_starting",
        questdb=f"{settings.questdb_host}:{settings.questdb_port}",
        channel=settings.redis_channel,
        model_dir=str(settings.model_dir),
    )
    await service.run(stop)
    log.info("regime_detector_stopped")
    return 0


def run() -> None:
    """Container entry point (``python hmm.py``)."""
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        configure_logging("INFO")
        log.critical("invalid_configuration", error=str(exc))
        sys.exit(EXIT_CONFIG_ERROR)
    configure_logging(settings.log_level)
    sys.exit(asyncio.run(amain(settings)))
