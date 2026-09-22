"""Entrypoint: ``python -m execution_motor``. Reads config from env, builds the motor and
serves ``ExecutionMotor.Execute``/``Health`` over mutual TLS. See README.md for every env
var and its fail-closed default.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import structlog

from .aegis_channel import open_aegis_channel
from .aegis_reporter import AegisReporter, GrpcReportTransport
from .alpaca import alpaca_broker_from_env
from .broker import Broker
from .config import MotorConfig
from .errors import ConfigError
from .grpc_service import ExecutionMotorServicer
from .halt import KillSwitch
from .keys import load_attestation_keys
from .mock_broker import MockBroker
from .motor import ExecutionMotor
from .server import build_grpc_server
from .server_config import ServerConfig
from .sor import RouterConfig, SmartOrderRouter, UnscoredPolicy
from .verifiers import AegisAttestationVerifier

_log = structlog.get_logger("execution_motor.main")

_GENERATED_DIR: Path = Path(__file__).resolve().parents[2] / "shared" / "generated"
_PROTO_MODULES = ("aegis_pb2", "aegis_pb2_grpc", "execution_motor_pb2", "execution_motor_pb2_grpc")


def _load_protos(directory: Path = _GENERATED_DIR) -> dict[str, ModuleType]:
    """Import the generated stubs (``shared/proto/generate.sh`` output)."""
    import importlib

    path = str(directory)
    if path not in sys.path:
        sys.path.insert(0, path)
    return {name: importlib.import_module(name) for name in _PROTO_MODULES}


def _equity_provider(broker: Broker) -> Callable[[], Decimal]:
    def _equity() -> Decimal:
        return broker.get_account().equity

    return _equity


def _build_broker(server_cfg: ServerConfig, env: dict[str, str]) -> Broker:
    if server_cfg.use_mock_broker:
        _log.warning("using_mock_broker", note="MOTOR_USE_MOCK_BROKER=1: development only")
        return MockBroker()
    return alpaca_broker_from_env(env)


def _build_reporter(
    server_cfg: ServerConfig, protos: dict[str, ModuleType], broker: Broker
) -> AegisReporter | None:
    if server_cfg.aegis_target is None:
        _log.warning("aegis_reporting_disabled", note="AEGIS_TARGET is unset")
        return None
    channel = open_aegis_channel(server_cfg.aegis_target, server_cfg.aegis_tls)
    stub = protos["aegis_pb2_grpc"].AegisStub(channel)
    transport = GrpcReportTransport(stub)
    return AegisReporter(
        transport,
        protos["aegis_pb2"].ExecutionReport,
        equity_provider=_equity_provider(broker),
    )


def build_app(env: dict[str, str]) -> tuple[ServerConfig, ExecutionMotorServicer, ModuleType]:
    """Wire everything from env. Raises ``ConfigError`` on any invalid/missing setting."""
    server_cfg = ServerConfig.from_env(env)
    motor_cfg = MotorConfig.from_env(env)
    protos = _load_protos()

    broker = _build_broker(server_cfg, env)
    keys = load_attestation_keys(server_cfg.attestation_keys_file)
    verifier = AegisAttestationVerifier(keys, production=motor_cfg.is_production)
    kill = KillSwitch(start_halted=False)
    router = SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.DENY))

    motor = ExecutionMotor(
        config=motor_cfg,
        brokers={broker.venue: broker},
        router=router,
        kill_switch=kill,
        verifier=verifier,
    )
    reporter = _build_reporter(server_cfg, protos, broker)
    servicer = ExecutionMotorServicer(
        motor, protos["execution_motor_pb2"], kill_switch=kill, reporter=reporter
    )
    return server_cfg, servicer, protos["execution_motor_pb2_grpc"]


def main() -> None:
    env = dict(os.environ)
    try:
        server_cfg, servicer, motor_pb2_grpc = build_app(env)
    except ConfigError as exc:
        _log.critical("startup_config_error", error=str(exc))
        raise SystemExit(1) from exc

    server = build_grpc_server(
        servicer, motor_pb2_grpc.add_ExecutionMotorServicer_to_server, server_cfg
    )
    server.start()
    _log.info(
        "execution_motor_started",
        listen=server_cfg.listen_addr,
        environment=server_cfg.environment,
    )
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=5)


if __name__ == "__main__":
    main()
