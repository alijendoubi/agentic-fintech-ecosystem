"""Synchronous client channel to Aegis, for ``AegisReporter`` (``Aegis.ReportExecution``).

Mirrors the ``AEGIS_CLIENT_TLS_CA/CERT/KEY`` env-var pattern that
``zone-a/cognitive-core/sinks.py`` uses for its (asyncio) Aegis channel, kept consistent on
purpose so an operator configures the same three variable names for every Aegis client in
the platform. This module is independent of that one (zone-b must not import zone-a code)
and uses a plain synchronous ``grpc`` channel: the execution-motor server itself is a
synchronous (``ThreadPoolExecutor``-backed) gRPC server, not asyncio.

``ServerConfig.from_env`` already refuses a production config with no ``AEGIS_CLIENT_TLS_CA``
(an insecure reporting channel must never carry fills/equity in production), so by the time
this function is called with ``tls=None`` the caller is already known to be non-production.
"""

from __future__ import annotations

from pathlib import Path

import grpc

from .server_config import AegisClientTls


def _read_pem(path: Path | None) -> bytes | None:
    if path is None:
        return None
    return path.read_bytes()


# HTTP/2 keepalive so a silently dead connection breaks the long-lived, mostly idle
# WatchKillSwitchState stream (which then reads as HARD) instead of leaving a stale NORMAL.
# max_pings_without_data=0 is required: the state stream carries no data between changes.
# NOT verified against the real Aegis (tonic) server's ping policy.
KEEPALIVE_OPTIONS: tuple[tuple[str, int], ...] = (
    ("grpc.keepalive_time_ms", 20_000),
    ("grpc.keepalive_timeout_ms", 10_000),
    ("grpc.http2.max_pings_without_data", 0),
)


def open_aegis_channel(target: str, tls: AegisClientTls | None) -> grpc.Channel:
    """Build the channel used for ``ReportExecution`` and ``WatchKillSwitchState``.
    ``tls=None`` is plaintext, dev only (refused in production before this is ever
    reached; see ``server_config.py``)."""
    if tls is None:
        return grpc.insecure_channel(target, options=KEEPALIVE_OPTIONS)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=_read_pem(tls.ca),
        certificate_chain=_read_pem(tls.cert),
        private_key=_read_pem(tls.key),
    )
    return grpc.secure_channel(target, credentials, options=KEEPALIVE_OPTIONS)
