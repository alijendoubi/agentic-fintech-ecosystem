"""Where finished signals go. Pluggable so the runner never depends on a transport.

* `InMemorySink`: collects signals (tests, dry runs that need inspection).
* `LogSink`: logs the signal and sends nothing (dev / paper wiring without Aegis).
* `AegisGrpcSink`: `Aegis.SubmitSignal` over gRPC using the shared protos and
  `proto_mapping.to_proto`. Generated stubs are injected (`pb2`, `stub`), so importing this
  module needs no grpc and no generated code.

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
