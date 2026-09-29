"""Entrypoint: ``python -m hitl_backend``. Config from env (see README), fail closed on errors."""

from __future__ import annotations

import os
import sys
from typing import Any

import structlog

from .attest import ApprovalAttestor
from .auth import Authenticator
from .config import ConfigError, Settings
from .grpc_clients import AegisHolds, MotorRelay, load_protos, open_channel
from .http_api import build_server
from .retained import RetainedContexts
from .service import HitlService, Policy

log = structlog.get_logger("hitl_backend.main")


def build(env: dict[str, str]) -> tuple[Settings, Any]:
    settings = Settings.from_env(env)
    pb = load_protos(env.get("AFE_PROTO_DIR", "").strip() or None)
    from afe_audit import (  # type: ignore[import-not-found]
        AuditIntegrityError,
        AuditLogger,
        RecordLookup,
    )
    from afe_audit.db import DsnConnectionSource  # type: ignore[import-not-found]

    source = DsnConnectionSource.from_env(env)  # POSTGRES_* as afe_audit_app
    audit = AuditLogger(source)
    retained = RetainedContexts(RecordLookup(source), pb, integrity_errors=(AuditIntegrityError,))
    attestor = None
    if settings.attestor_id is not None and settings.attestor_key_file is not None:
        attestor = ApprovalAttestor.from_seed_file(
            settings.attestor_id, settings.attestor_key_file, issuer=settings.jwt_issuer
        )
        log.info(
            "approval_attestor_loaded",
            attestor_id=attestor.attestor_id,
            public_key_hex=attestor.public_key_hex(),  # what Aegis's hold_attestors must list
        )
    aegis_stub = pb["aegis_pb2_grpc"].AegisStub(
        open_channel(settings.aegis_target, settings.aegis_tls)
    )
    holds = AegisHolds(
        aegis_stub, pb, identity=settings.aegis_identity, timeout_s=settings.aegis_timeout_s
    )
    relay = None
    if settings.motor_target is not None:
        channel = open_channel(settings.motor_target, settings.motor_tls)
        relay = MotorRelay(
            pb["execution_motor_pb2_grpc"].ExecutionMotorStub(channel),
            timeout_s=settings.motor_timeout_s,
        )
    service = HitlService(
        holds=holds,
        audit=_Actor(audit, settings.audit_actor),
        pb=pb,
        policy=Policy(
            quantity_threshold=settings.four_eyes_quantity_threshold,
            notional_threshold_usd=settings.four_eyes_notional_threshold_usd,
            cooling_period_s=settings.cooling_period_s,
        ),
        relay=relay,
        attestor=attestor,
        retained=retained,
    )
    auth = Authenticator(
        settings.jwt_secret,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        service_token=settings.service_token,
    )
    return settings, build_server(settings.listen_host, settings.listen_port, service, auth)


class _Actor:
    """Audit records name the human (JWT sub) and, in the payload, this service."""

    def __init__(self, logger: Any, service: str) -> None:
        self._logger = logger
        self._service = service

    def record(self, event_type: str, actor: str, payload: dict[str, object]) -> Any:
        return self._logger.record(event_type, actor, {**payload, "via": self._service})


def main() -> None:
    try:
        settings, server = build(dict(os.environ))
    except ConfigError as exc:
        log.critical("startup_config_error", error=str(exc))
        sys.exit(2)
    log.info(
        "hitl_backend_started",
        listen=f"{settings.listen_host}:{settings.listen_port}",
        production=settings.production,
        motor_relay=settings.motor_target is not None,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
