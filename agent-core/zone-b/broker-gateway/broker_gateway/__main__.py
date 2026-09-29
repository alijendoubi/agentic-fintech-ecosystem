"""Entrypoint: ``python -m broker_gateway``. Reads env (see README.md), serves
``BrokerGatewayService`` over mutual TLS. Any configuration problem exits with status 1
before the port is opened.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
from concurrent import futures
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Final

import grpc
import structlog
from execution_motor.errors import ConfigError
from execution_motor.keys import load_attestation_keys
from execution_motor.verifiers import AegisAttestationVerifier

from .alpaca import AlpacaForwarder
from .authorize import SubmitAuthoriser
from .config import GatewayConfig, TlsPaths
from .service import BrokerGatewayServicer

_log = structlog.get_logger("broker_gateway.main")
_MAX_WORKERS: Final = 8


def _repo_generated_dir() -> Path | None:
    """agent-core/shared/generated when run from a checkout (the image uses PYTHONPATH)."""
    parents = Path(__file__).resolve().parents
    return parents[3] / "shared" / "generated" if len(parents) > 3 else None


def load_protos() -> tuple[ModuleType, ModuleType]:
    """Import the generated stubs (``shared/proto/generate.sh`` output)."""
    directory = _repo_generated_dir()
    if importlib.util.find_spec("broker_gateway_pb2") is None and directory is not None:
        sys.path.insert(0, str(directory))
    return (
        importlib.import_module("broker_gateway_pb2"),
        importlib.import_module("broker_gateway_pb2_grpc"),
    )


@dataclass(frozen=True)
class GatewayApp:
    config: GatewayConfig
    servicer: BrokerGatewayServicer
    pb2_grpc: ModuleType


def build_app(env: dict[str, str]) -> GatewayApp:
    """Wire everything from env. Raises ``ConfigError`` on anything invalid or missing."""
    config = GatewayConfig.from_env(env)
    keys = load_attestation_keys(config.attestation_keys_file)
    verifier = AegisAttestationVerifier(keys, production=config.is_production)
    pb2, pb2_grpc = load_protos()
    authoriser = SubmitAuthoriser(
        verifier,
        max_ttl_ns=config.max_ttl_ns,
        max_clock_skew_ns=config.max_clock_skew_ns,
        started_at_ns=time.time_ns(),
        clock_ns=time.time_ns,
    )
    forwarder = AlpacaForwarder(
        config.credentials, base_url=config.base_url, timeout_s=config.broker_timeout_s
    )
    servicer = BrokerGatewayServicer(
        pb2,
        authoriser=authoriser,
        forwarder=forwarder,
        allowed_client_cns=config.allowed_client_cns,
    )
    return GatewayApp(config=config, servicer=servicer, pb2_grpc=pb2_grpc)


def server_credentials(tls: TlsPaths) -> grpc.ServerCredentials:
    """Client certificates are REQUIRED; there is no plaintext or optional-auth mode."""
    return grpc.ssl_server_credentials(
        [(tls.key.read_bytes(), tls.cert.read_bytes())],
        root_certificates=tls.client_ca.read_bytes(),
        require_client_auth=True,
    )


def build_server(app: GatewayApp, *, listen_addr: str | None = None) -> tuple[grpc.Server, int]:
    """Build (not start) the server; returns it and the bound port."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS))
    app.pb2_grpc.add_BrokerGatewayServiceServicer_to_server(app.servicer, server)
    port = server.add_secure_port(
        listen_addr or app.config.listen_addr, server_credentials(app.config.tls)
    )
    if port == 0:
        raise ConfigError(f"could not bind {listen_addr or app.config.listen_addr}")
    return server, port


def main() -> None:
    try:
        app = build_app(dict(os.environ))
        server, _ = build_server(app)
    except (ConfigError, OSError) as exc:
        # str(exc) never contains a credential value: config errors name variables only.
        _log.critical("startup_config_error", error=str(exc))
        raise SystemExit(1) from None
    server.start()
    _log.info(
        "broker_gateway_started",
        listen=app.config.listen_addr,
        environment=app.config.environment,
        broker_base_url=app.config.base_url,
        allowed_client_cns=sorted(app.config.allowed_client_cns),
    )
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=5)


if __name__ == "__main__":
    main()
