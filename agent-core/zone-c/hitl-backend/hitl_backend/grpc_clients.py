"""mTLS channels and thin stubs for Aegis (ListHolds/GetHold/ResolveHold) and execution-motor."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import grpc

from .config import ConfigError, TlsFiles

PROTO_MODULES = (
    "aegis_pb2",
    "aegis_pb2_grpc",
    "trade_signal_pb2",
    "market_snapshot_pb2",
    "compliance_manifest_pb2",
    "execution_motor_pb2",
    "execution_motor_pb2_grpc",
)


def load_protos(directory: str | None) -> dict[str, ModuleType]:
    if directory:
        path = str(Path(directory))
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        return {name: importlib.import_module(name) for name in PROTO_MODULES}
    except ImportError as exc:
        raise ConfigError(
            f"generated protobuf stubs not importable ({exc}); set AFE_PROTO_DIR"
        ) from exc


def open_channel(target: str, tls: TlsFiles | None) -> grpc.Channel:
    if tls is None:
        return grpc.insecure_channel(target)  # config refuses this in production
    try:
        creds = grpc.ssl_channel_credentials(
            root_certificates=tls.ca.read_bytes(),
            private_key=tls.key.read_bytes(),
            certificate_chain=tls.cert.read_bytes(),
        )
    except OSError as exc:
        raise ConfigError(f"cannot read TLS material: {exc}") from exc
    return grpc.secure_channel(target, creds)


class AegisHolds:
    """The three hold RPCs, with this service's certificate identity as ``operator_id`` (Aegis
    binds ``operator_id`` to the caller's certificate CN)."""

    def __init__(
        self, stub: Any, pb: dict[str, ModuleType], *, identity: str, timeout_s: float
    ) -> None:
        self._stub = stub
        self._pb = pb
        self.identity = identity
        self._timeout = timeout_s

    def list(self) -> list[Any]:
        reply = self._stub.ListHolds(self._pb["aegis_pb2"].Empty(), timeout=self._timeout)
        return list(reply.holds)

    def get(self, hold_id: str) -> Any:
        return self._stub.GetHold(
            self._pb["aegis_pb2"].GetHoldRequest(hold_id=hold_id), timeout=self._timeout
        )

    def resolve(self, **fields: Any) -> Any:
        request = self._pb["aegis_pb2"].ResolveHoldRequest(operator_id=self.identity, **fields)
        return self._stub.ResolveHold(request, timeout=self._timeout)


class MotorRelay:
    def __init__(self, stub: Any, *, timeout_s: float) -> None:
        self._stub = stub
        self._timeout = timeout_s

    def execute(self, request: Any) -> Any:
        return self._stub.ExecuteWithContext(request, timeout=self._timeout)
