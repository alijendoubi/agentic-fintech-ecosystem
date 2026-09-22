"""gRPC server bootstrap: mutual TLS, mirroring ``agent-core/zone-b/aegis/src/server.rs``
in spirit for Python.

Server certificate + key and the CLIENT CA come from files (``ServerConfig.tls``). The
client CA is mandatory and client certificates are REQUIRED
(``require_client_auth=True``, no optional client auth): a connection without a
certificate chaining to that CA never reaches a handler.

``config.tls is None`` is plaintext, dev-only. ``ServerConfig.from_env`` (see
``server_config.py``) already refuses to build that configuration unless
``MOTOR_INSECURE_DEV=1`` AND the environment is not production AND the listen address is
an explicit loopback address, so by the time ``build_grpc_server`` is called with
``tls=None`` those preconditions are already known to hold.
"""

from __future__ import annotations

from concurrent import futures
from pathlib import Path
from typing import Any, Final

import grpc
import structlog

from .server_config import ServerConfig, TlsPaths

_log = structlog.get_logger("execution_motor.server")
DEFAULT_MAX_WORKERS: Final = 16


def _read(path: Path) -> bytes:
    return path.read_bytes()


def build_server_credentials(tls: TlsPaths) -> grpc.ServerCredentials:
    cert = _read(tls.cert)
    key = _read(tls.key)
    client_ca = _read(tls.client_ca)
    return grpc.ssl_server_credentials(
        [(key, cert)],
        root_certificates=client_ca,
        require_client_auth=True,
    )


def build_grpc_server(
    servicer: Any,
    add_servicer_to_server: Any,
    config: ServerConfig,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> grpc.Server:
    """Build (but do not start) the server, bound to ``config.listen_addr``."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    add_servicer_to_server(servicer, server)
    if config.tls is not None:
        server.add_secure_port(config.listen_addr, build_server_credentials(config.tls))
    else:
        _log.warning(
            "serving_without_tls",
            listen=config.listen_addr,
            note="MOTOR_INSECURE_DEV=1: development only, never production",
        )
        server.add_insecure_port(config.listen_addr)
    return server
