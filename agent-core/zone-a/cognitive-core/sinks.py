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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import structlog

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


def open_aegis_channel(host: str, port: int) -> Any:  # pragma: no cover - needs grpc + network
    """Insecure aio channel on the zone-a<->zone-b internal network.

    TODO(owner): mutual TLS between cognitive-core and Aegis before any production use.
    """
    import grpc

    return grpc.aio.insecure_channel(f"{host}:{port}")
