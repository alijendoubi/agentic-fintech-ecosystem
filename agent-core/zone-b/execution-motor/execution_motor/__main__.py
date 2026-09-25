"""Entrypoint: ``python -m execution_motor``. Reads config from env, builds the motor and
serves ``ExecutionMotor.Execute``/``Health`` over mutual TLS. See README.md for every env
var and its fail-closed default.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import grpc
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
from .kill_watch import KillSwitchWatcher, aegis_state_stream, build_kill_guard
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


@dataclass(frozen=True)
class MotorApp:
    server_cfg: ServerConfig
    servicer: ExecutionMotorServicer
    motor_pb2_grpc: ModuleType
    # None only outside production with AEGIS_TARGET unset (production refuses that).
    kill_watcher: KillSwitchWatcher | None


def _build_reporter(
    protos: dict[str, ModuleType], channel: grpc.Channel, broker: Broker
) -> AegisReporter:
    stub = protos["aegis_pb2_grpc"].AegisStub(channel)
    transport = GrpcReportTransport(stub)
    return AegisReporter(
        transport,
        protos["aegis_pb2"].ExecutionReport,
        equity_provider=_equity_provider(broker),
    )


def _build_kill_guard(
    protos: dict[str, ModuleType], channel: grpc.Channel | None, brokers: dict[str, Broker]
) -> tuple[KillSwitch, KillSwitchWatcher | None]:
    if channel is None:
        _log.warning(
            "kill_switch_watch_disabled",
            note="AEGIS_TARGET is unset: Aegis kill-switch trips are invisible (dev only)",
        )
        return KillSwitch(start_halted=False), None
    stub = protos["aegis_pb2_grpc"].AegisStub(channel)
    return build_kill_guard(aegis_state_stream(stub, protos["aegis_pb2"].Empty), brokers)


def build_app(env: dict[str, str]) -> MotorApp:
    """Wire everything from env. Raises ``ConfigError`` on any invalid/missing setting."""
    server_cfg = ServerConfig.from_env(env)
    motor_cfg = MotorConfig.from_env(env)
    protos = _load_protos()

    broker = _build_broker(server_cfg, env)
    brokers = {broker.venue: broker}
    keys = load_attestation_keys(server_cfg.attestation_keys_file)
    verifier = AegisAttestationVerifier(keys, production=motor_cfg.is_production)
    channel = (
        None
        if server_cfg.aegis_target is None
        else open_aegis_channel(server_cfg.aegis_target, server_cfg.aegis_tls)
    )
    kill, watcher = _build_kill_guard(protos, channel, brokers)
    router = SmartOrderRouter(RouterConfig(unscored_policy=UnscoredPolicy.DENY))

    motor = ExecutionMotor(
        config=motor_cfg,
        brokers=brokers,
        router=router,
        kill_switch=kill,
        verifier=verifier,
    )
    if channel is None:
        _log.warning("aegis_reporting_disabled", note="AEGIS_TARGET is unset")
    reporter = None if channel is None else _build_reporter(protos, channel, broker)
    servicer = ExecutionMotorServicer(
        motor, protos["execution_motor_pb2"], kill_switch=kill, reporter=reporter
    )
    return MotorApp(server_cfg, servicer, protos["execution_motor_pb2_grpc"], watcher)


def main() -> None:
    env = dict(os.environ)
    try:
        app = build_app(env)
    except ConfigError as exc:
        _log.critical("startup_config_error", error=str(exc))
        raise SystemExit(1) from exc

    if app.kill_watcher is not None:
        app.kill_watcher.start()  # before serving: the switch reads HARD until Aegis answers
    server = build_grpc_server(
        app.servicer, app.motor_pb2_grpc.add_ExecutionMotorServicer_to_server, app.server_cfg
    )
    server.start()
    _log.info(
        "execution_motor_started",
        listen=app.server_cfg.listen_addr,
        environment=app.server_cfg.environment,
        kill_switch_watch=app.kill_watcher is not None,
    )
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(grace=5)
    finally:
        if app.kill_watcher is not None:
            app.kill_watcher.stop()


if __name__ == "__main__":
    main()
