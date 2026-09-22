"""Where finished signals go. Pluggable so the runner never depends on a transport.

* `InMemorySink`: collects signals (tests, dry runs that need inspection).
* `LogSink`: logs the signal and sends nothing (dev / paper wiring without Aegis).
* `AegisGrpcSink`: `Aegis.SubmitSignal` over gRPC using the shared protos and
  `proto_mapping.to_proto`. Generated stubs are injected (`pb2`, `stub`), so importing this
  module needs no grpc and no generated code.
* `AegisRelaySink`: `AegisGrpcSink` plus the execution-motor relay (phase_3_aegis_execution.md
  section 5): `cognitive-core --SubmitSignal(TradeSignal)--> AEGIS --AegisDecision(+attestation)
  --> execution-motor`. Only an APPROVED decision carrying an `Attestation` is forwarded; a
  held or rejected decision is logged and stops here, by design (Aegis is the only authority
  that may let a signal reach execution).
* `MotorGrpcSink` / `InMemoryMotorSink`: the execution-motor leg, mirroring the Aegis sink
  shapes above. `execution_motor.proto` (the service definition, `Execute(AegisDecision)
  returns (ExecuteAck)`) does not exist in this worktree yet (PKG-X3, branch
  `pkg-x3/motor-service`), so `MotorStub` is a minimal local Protocol standing in for the
  generated `execution_motor_pb2_grpc` stub. The request type needs no stand-in: it is the
  already-generated `aegis_pb2.AegisDecision`. Swap `MotorStub` for the real generated stub
  once `execution_motor.proto` lands; `MotorGrpcSink` itself needs no change.

Delivery is at-most-once: a failed send raises `SinkError` and is NOT retried, because a
late trade signal is worse than a missed one (TradeSignal has a short `valid_until_ns`).
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import structlog

from .config import ConfigError
from .models import TradeSignal
from .proto_mapping import to_proto

log = structlog.get_logger(__name__)

DEFAULT_AEGIS_TIMEOUT_S = 1.0
DEFAULT_GENERATED_DIR = Path(__file__).resolve().parents[2] / "shared" / "generated"
PROTO_MODULES = ("trade_signal_pb2", "aegis_pb2", "aegis_pb2_grpc")
MOTOR_PROTO_MODULES = ("execution_motor_pb2", "execution_motor_pb2_grpc")
_ERROR_LOG_CHARS = 200


class SinkError(RuntimeError):
    """The signal could not be delivered. The runner counts it and moves on."""


@dataclass(frozen=True, slots=True)
class SinkReceipt:
    """What the downstream said about the signal (informational; never changes state)."""

    accepted: bool
    detail: str = ""


class SignalSink(Protocol):
    async def send(self, signal: TradeSignal) -> SinkReceipt: ...


@dataclass
class InMemorySink:
    sent: list[TradeSignal] = field(default_factory=list)
    fail_with: Exception | None = None

    async def send(self, signal: TradeSignal) -> SinkReceipt:
        if self.fail_with is not None:
            raise SinkError(str(self.fail_with)) from self.fail_with
        self.sent.append(signal)
        return SinkReceipt(True, "in-memory")


class LogSink:
    """Dry-run sink: nothing leaves the process."""

    async def send(self, signal: TradeSignal) -> SinkReceipt:
        log.info(
            "signal_dry_run",
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side=signal.side.value,
            status=signal.status.value,
            omega=signal.omega,
        )
        return SinkReceipt(False, "dry-run: not sent")


def load_generated_protos(directory: str | Path | None = None) -> dict[str, ModuleType]:
    """Import the generated stubs (`shared/proto/generate.sh` output). Raises ImportError."""
    path = str(Path(directory) if directory is not None else DEFAULT_GENERATED_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    return {name: importlib.import_module(name) for name in PROTO_MODULES}


def load_generated_motor_protos(directory: str | Path | None = None) -> dict[str, ModuleType]:
    """Import the generated execution-motor stubs. Raises ImportError.

    `execution_motor.proto` (PKG-X3, branch `pkg-x3/motor-service`) does not exist in this
    worktree, so `execution_motor_pb2_grpc.py` is never produced by `generate.sh` today: this
    always raises ImportError until that lands. Callers (see `__main__.build_sink`) treat that
    as "the motor relay is not available yet" and must not let it block the Aegis-only path.
    """
    path = str(Path(directory) if directory is not None else DEFAULT_GENERATED_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    return {name: importlib.import_module(name) for name in MOTOR_PROTO_MODULES}


class AegisGrpcSink:
    """Submit signals to Aegis. `stub` is an `aegis_pb2_grpc.AegisStub` (aio channel)."""

    def __init__(
        self,
        *,
        stub: Any,
        trade_signal_pb2: Any,
        strategy_id: str,
        timeout_s: float = DEFAULT_AEGIS_TIMEOUT_S,
    ) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id must not be empty (Aegis regime gate C18 needs it)")
        if not timeout_s > 0:
            raise ValueError("timeout_s must be positive")
        self._stub = stub
        self._pb2 = trade_signal_pb2
        self._strategy_id = strategy_id
        self._timeout_s = timeout_s

    async def send(self, signal: TradeSignal) -> SinkReceipt:
        try:
            message = to_proto(signal, self._pb2, strategy_id=self._strategy_id)
            decision = await self._stub.SubmitSignal(message, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - any transport/encoding failure is a SinkError
            raise SinkError(f"SubmitSignal failed: {type(exc).__name__}: {exc}") from exc
        detail = _describe_decision(decision)
        log.info("signal_submitted", signal_id=signal.signal_id, aegis=detail)
        return SinkReceipt(True, detail)


def _describe_decision(decision: Any) -> str:
    try:
        name = (
            decision.DESCRIPTOR.fields_by_name["decision"]
            .enum_type.values_by_number[decision.decision]
            .name
        )
        return f"{name} reasons={list(decision.reasons)}"
    except (AttributeError, KeyError):
        return "decision unreadable"


def _decision_status_name(decision: Any) -> str:
    try:
        return str(
            decision.DESCRIPTOR.fields_by_name["decision"]
            .enum_type.values_by_number[decision.decision]
            .name
        )
    except (AttributeError, KeyError):
        return ""


def _decision_is_approved_with_attestation(decision: Any) -> bool:
    """True only for an APPROVED decision that actually carries an `Attestation`.

    Only this combination may reach execution-motor (phase_3_aegis_execution.md section 5,
    Appendix A.1: `attestation` is "set only when APPROVED"). A HELD_FOR_HUMAN or REJECTED
    decision, or an APPROVED one with no attestation set, is never forwarded.
    """
    if _decision_status_name(decision) != "DECISION_APPROVED":
        return False
    has_field = getattr(decision, "HasField", None)
    if callable(has_field):
        try:
            return bool(has_field("attestation"))
        except ValueError:
            return False
    return getattr(decision, "attestation", None) is not None


class MotorSink(Protocol):
    """Where an approved `AegisDecision` goes next. Mirrors `SignalSink`."""

    async def send(self, decision: Any) -> SinkReceipt: ...


@dataclass
class InMemoryMotorSink:
    """Fake motor sink for tests: collects the exact `AegisDecision` objects it received."""

    sent: list[Any] = field(default_factory=list)
    fail_with: Exception | None = None

    async def send(self, decision: Any) -> SinkReceipt:
        if self.fail_with is not None:
            raise SinkError(str(self.fail_with)) from self.fail_with
        self.sent.append(decision)
        return SinkReceipt(True, "in-memory")


DEFAULT_MOTOR_TIMEOUT_S = 1.0


class MotorStub(Protocol):
    """Minimal local stand-in for the generated `execution_motor_pb2_grpc` stub.

    TODO(owner): `execution_motor.proto` (PKG-X3, branch `pkg-x3/motor-service`) does not
    exist in this worktree yet, so there is no generated `ExecutionMotorStub` to inject.
    Once it lands and `shared/proto/generate.sh` produces `execution_motor_pb2_grpc.py`,
    swap the real `ExecutionMotorStub(channel)` in for this Protocol wherever a `MotorStub`
    is constructed (see `__main__.build_sink`); `MotorGrpcSink` itself needs no change since
    it only calls `.Execute(decision, timeout=...)`, the same shape the real stub will have.
    """

    async def Execute(self, decision: Any, *, timeout: float) -> Any: ...  # noqa: N802


class MotorGrpcSink:
    """Relay an approved `AegisDecision` to execution-motor via `Execute`. `stub` is injected
    (see `MotorStub`) so this class has no import-time dependency on generated code.
    """

    def __init__(self, *, stub: MotorStub, timeout_s: float = DEFAULT_MOTOR_TIMEOUT_S) -> None:
        if not timeout_s > 0:
            raise ValueError("timeout_s must be positive")
        self._stub = stub
        self._timeout_s = timeout_s

    async def send(self, decision: Any) -> SinkReceipt:
        try:
            ack = await self._stub.Execute(decision, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - any transport/encoding failure is a SinkError
            raise SinkError(f"Execute failed: {type(exc).__name__}: {exc}") from exc
        detail = _describe_ack(ack)
        log.info(
            "decision_relayed", signal_id=getattr(decision, "signal_id", ""), motor=detail
        )
        return SinkReceipt(True, detail)


def _describe_ack(ack: Any) -> str:
    accepted = getattr(ack, "accepted", None)
    if accepted is None:
        return "ack unreadable"
    detail = getattr(ack, "detail", "")
    return f"accepted={accepted} detail={detail!r}"


class AegisRelaySink:
    """`AegisGrpcSink` plus the execution-motor relay (see module docstring).

    `send(signal)` always submits to Aegis first; the returned `SinkReceipt` always reflects
    the Aegis outcome. A HELD/REJECTED decision (or an APPROVED one without an attestation)
    is logged and never reaches `motor_sink`, by design. A failure relaying an APPROVED
    decision to execution-motor raises `SinkError`, so it is never swallowed: it flows through
    the same `signal_send_failed` / `CycleOutcome.SINK_FAILED` path the runner already uses
    for any other undelivered signal (see `service.CognitiveRunner._deliver`).
    """

    def __init__(
        self,
        *,
        aegis_stub: Any,
        trade_signal_pb2: Any,
        strategy_id: str,
        motor_sink: MotorSink,
        aegis_timeout_s: float = DEFAULT_AEGIS_TIMEOUT_S,
    ) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id must not be empty (Aegis regime gate C18 needs it)")
        if not aegis_timeout_s > 0:
            raise ValueError("aegis_timeout_s must be positive")
        self._stub = aegis_stub
        self._pb2 = trade_signal_pb2
        self._strategy_id = strategy_id
        self._motor = motor_sink
        self._timeout_s = aegis_timeout_s

    async def send(self, signal: TradeSignal) -> SinkReceipt:
        try:
            message = to_proto(signal, self._pb2, strategy_id=self._strategy_id)
            decision = await self._stub.SubmitSignal(message, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - any transport/encoding failure is a SinkError
            raise SinkError(f"SubmitSignal failed: {type(exc).__name__}: {exc}") from exc
        detail = _describe_decision(decision)
        log.info("signal_submitted", signal_id=signal.signal_id, aegis=detail)
        if not _decision_is_approved_with_attestation(decision):
            log.info(
                "motor_relay_skipped",
                signal_id=signal.signal_id,
                aegis_decision=_decision_status_name(decision) or "UNKNOWN",
            )
            return SinkReceipt(True, detail)
        try:
            await self._motor.send(decision)
        except SinkError as exc:
            log.error(
                "motor_relay_failed",
                signal_id=signal.signal_id,
                aegis_decision=detail,
                error=str(exc)[:_ERROR_LOG_CHARS],
            )
            raise SinkError(f"motor relay failed after aegis approval: {exc}") from exc
        return SinkReceipt(True, detail)


ENVIRONMENT_VAR = "ENVIRONMENT"
TLS_CA_VAR = "AEGIS_CLIENT_TLS_CA"
TLS_CERT_VAR = "AEGIS_CLIENT_TLS_CERT"
TLS_KEY_VAR = "AEGIS_CLIENT_TLS_KEY"


@dataclass(frozen=True, slots=True)
class AegisTlsFiles:
    """PEM file paths for the Aegis channel. `cert` and `key` are both set (mTLS) or both None."""

    ca: Path | None
    cert: Path | None
    key: Path | None


def aegis_tls_from_env(env: Mapping[str, str]) -> AegisTlsFiles | None:
    """Read `AEGIS_CLIENT_TLS_CA/CERT/KEY` (PEM file paths). None means "no TLS configured".

    Raises `ConfigError` when the client cert and key are not given together, and, when
    `ENVIRONMENT=production`, unless a CA is configured: an insecure channel must never
    carry trade signals in production.
    """
    values = {name: env.get(name, "").strip() for name in (TLS_CA_VAR, TLS_CERT_VAR, TLS_KEY_VAR)}
    ca, cert, key = (Path(v) if v else None for v in values.values())
    if (cert is None) != (key is None):
        raise ConfigError(f"{TLS_CERT_VAR} and {TLS_KEY_VAR} must be set together")
    production = env.get(ENVIRONMENT_VAR, "").strip().lower() == "production"
    if production and ca is None:
        raise ConfigError(
            f"ENVIRONMENT=production refuses an insecure Aegis channel: set {TLS_CA_VAR} "
            f"(and {TLS_CERT_VAR}/{TLS_KEY_VAR} for mutual TLS)"
        )
    if ca is None and cert is None:
        return None
    return AegisTlsFiles(ca=ca, cert=cert, key=key)


def _read_pem(var: str, path: Path | None) -> bytes | None:
    if path is None:
        return None
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"{var} is not readable: {type(exc).__name__}") from exc


def open_aegis_channel(host: str, port: int, env: Mapping[str, str] | None = None) -> Any:
    """aio channel to Aegis: TLS/mTLS when `AEGIS_CLIENT_TLS_*` is set, else plaintext (dev only).

    Plaintext is refused with `ConfigError` when `ENVIRONMENT=production`. The env is read
    from `env` (default `os.environ`). TODO(owner): Aegis must be configured to require client
    certificates (mTLS) on its side; this only makes the client able to present one.
    """
    source = os.environ if env is None else env
    tls = aegis_tls_from_env(source)
    import grpc

    target = f"{host}:{port}"
    if tls is None:
        log.warning("aegis_channel_insecure", target=target)
        return grpc.aio.insecure_channel(target)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=_read_pem(TLS_CA_VAR, tls.ca),
        certificate_chain=_read_pem(TLS_CERT_VAR, tls.cert),
        private_key=_read_pem(TLS_KEY_VAR, tls.key),
    )
    return grpc.aio.secure_channel(target, credentials)


# ---- execution-motor channel: mirrors the Aegis TLS/channel setup above --------------------

MOTOR_TARGET_VAR = "MOTOR_TARGET"
MOTOR_TLS_CA_VAR = "MOTOR_CLIENT_TLS_CA"
MOTOR_TLS_CERT_VAR = "MOTOR_CLIENT_TLS_CERT"
MOTOR_TLS_KEY_VAR = "MOTOR_CLIENT_TLS_KEY"
# Placeholder "host:port" default; execution-motor has no Dockerfile/service/entrypoint yet
# (see agent-core/README.md "Current status"), so this port is not verified against a real
# deployment. TODO(owner): confirm the target once execution-motor exposes a gRPC server.
DEFAULT_MOTOR_TARGET = "execution-motor:50052"


def motor_tls_from_env(env: Mapping[str, str]) -> AegisTlsFiles | None:
    """Read `MOTOR_CLIENT_TLS_CA/CERT/KEY` (PEM file paths). Same rules as
    `aegis_tls_from_env`: client cert and key must be given together, and
    `ENVIRONMENT=production` refuses an insecure channel unless a CA is configured.
    """
    values = {
        name: env.get(name, "").strip()
        for name in (MOTOR_TLS_CA_VAR, MOTOR_TLS_CERT_VAR, MOTOR_TLS_KEY_VAR)
    }
    ca, cert, key = (Path(v) if v else None for v in values.values())
    if (cert is None) != (key is None):
        raise ConfigError(f"{MOTOR_TLS_CERT_VAR} and {MOTOR_TLS_KEY_VAR} must be set together")
    production = env.get(ENVIRONMENT_VAR, "").strip().lower() == "production"
    if production and ca is None:
        raise ConfigError(
            f"ENVIRONMENT=production refuses an insecure execution-motor channel: set "
            f"{MOTOR_TLS_CA_VAR} (and {MOTOR_TLS_CERT_VAR}/{MOTOR_TLS_KEY_VAR} for mutual TLS)"
        )
    if ca is None and cert is None:
        return None
    return AegisTlsFiles(ca=ca, cert=cert, key=key)


def open_motor_channel(target: str, env: Mapping[str, str] | None = None) -> Any:
    """aio channel to execution-motor: TLS/mTLS when `MOTOR_CLIENT_TLS_*` is set, else
    plaintext (dev only). Plaintext is refused with `ConfigError` when `ENVIRONMENT=production`.
    Mirrors `open_aegis_channel`.
    """
    source = os.environ if env is None else env
    tls = motor_tls_from_env(source)
    import grpc

    if tls is None:
        log.warning("motor_channel_insecure", target=target)
        return grpc.aio.insecure_channel(target)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=_read_pem(MOTOR_TLS_CA_VAR, tls.ca),
        certificate_chain=_read_pem(MOTOR_TLS_CERT_VAR, tls.cert),
        private_key=_read_pem(MOTOR_TLS_KEY_VAR, tls.key),
    )
    return grpc.aio.secure_channel(target, credentials)
