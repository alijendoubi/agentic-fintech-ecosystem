"""``Aegis.PushReferenceData`` client over mTLS.

Mirrors ``agent-core/zone-a/cognitive-core/sinks.py`` (``AegisGrpcSink`` /
``aegis_tls_from_env`` / ``open_aegis_channel``): generated protobuf stubs are
injected, never imported directly, so this module needs no grpc/generated code
to be importable; and TLS env vars follow the same shape, renamed for this
service's role (``market-data-writer``, see ``agent-core/zone-b/aegis/README.md``
"Identities file"):

* ``AEGIS_TARGET``            -- "host:port" (this service's own convention;
  cognitive-core instead takes host/port separately -- see README "Known
  limitations" for why this bridge uses one combined var).
* ``REFDATA_CLIENT_TLS_CA/CERT/KEY`` -- PEM file paths (mirrors
  ``AEGIS_CLIENT_TLS_CA/CERT/KEY`` in sinks.py).

Fail-closed: a request that cannot be delivered (channel down, timeout) raises
``PushError`` -- logged by the caller and retried next cycle, never crashing the
loop. A per-item rejection in the RPC response is NOT an exception: it is
returned in ``PushResult.rejected`` so the caller can log every rejection with
its reason and skip advancing that item's watermark.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import structlog

from refdata_bridge.batch import PendingBatch
from refdata_bridge.config import Settings

log = structlog.get_logger(__name__)

DEFAULT_GENERATED_DIR = Path(__file__).resolve().parents[3] / "shared" / "generated"
# market_snapshot_pb2 is needed too: aegis.proto imports market_snapshot.proto, and
# protoc generates RegimeLabelPacket (+ the RegimeLabel enum) in THAT file's module,
# not in aegis_pb2, even though aegis.proto references it.
PROTO_MODULES = ("aegis_pb2", "aegis_pb2_grpc", "market_snapshot_pb2")


class PushError(RuntimeError):
    """PushReferenceData could not be delivered (channel/timeout/transport failure)."""


@dataclass(frozen=True, slots=True)
class Rejection:
    key: str
    reason: str


@dataclass(frozen=True, slots=True)
class PushResult:
    applied_snapshot_symbols: list[str]
    regime_applied: bool
    rejected: list[Rejection] = field(default_factory=list)


def load_generated_protos(directory: str | Path | None = None) -> dict[str, ModuleType]:
    """Import the generated stubs (``shared/proto/generate.sh`` output). Raises ImportError."""
    path = str(Path(directory) if directory is not None else DEFAULT_GENERATED_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    return {name: importlib.import_module(name) for name in PROTO_MODULES}


def open_aegis_channel(settings: Settings) -> Any:
    """aio channel to Aegis: mTLS when ``REFDATA_CLIENT_TLS_*`` is set, else plaintext (dev only).

    Plaintext is refused before this is even called when ``ENVIRONMENT=production``
    (``Settings.from_env`` raises ``ConfigError`` first).
    """
    import grpc

    if settings.tls_ca is None and settings.tls_cert is None:
        log.warning("aegis_channel_insecure", target=settings.aegis_target)
        return grpc.aio.insecure_channel(settings.aegis_target)
    credentials = grpc.ssl_channel_credentials(
        root_certificates=_read_pem(settings.tls_ca),
        certificate_chain=_read_pem(settings.tls_cert),
        private_key=_read_pem(settings.tls_key),
    )
    return grpc.aio.secure_channel(settings.aegis_target, credentials)


def _read_pem(path: Path | None) -> bytes | None:
    if path is None:
        return None
    return path.read_bytes()


class AegisRefdataClient:
    """Calls ``Aegis.PushReferenceData``. ``stub`` is an ``aegis_pb2_grpc.AegisStub``.

    ``market_snapshot_pb2`` is the module holding ``RegimeLabelPacket`` /
    ``RegimeLabel``; when omitted it is imported lazily by module name (works once
    ``load_generated_protos`` has put the generated dir on ``sys.path``).
    """

    def __init__(
        self, *, stub: Any, aegis_pb2: Any, timeout_s: float, market_snapshot_pb2: Any = None
    ) -> None:
        if not timeout_s > 0:
            raise ValueError("timeout_s must be positive")
        self._stub = stub
        self._pb2 = aegis_pb2
        self._timeout_s = timeout_s
        if market_snapshot_pb2 is None:
            import market_snapshot_pb2 as _market_snapshot_pb2

            market_snapshot_pb2 = _market_snapshot_pb2
        self._snapshot_pb2 = market_snapshot_pb2

    async def push(self, batch: PendingBatch) -> PushResult:
        if not batch.snapshots and batch.regime is None:
            return PushResult(applied_snapshot_symbols=[], regime_applied=False)
        request = self._build_request(batch)
        try:
            response = await self._stub.PushReferenceData(request, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - any transport failure is a PushError
            raise PushError(f"PushReferenceData failed: {type(exc).__name__}: {exc}") from exc
        return self._parse_response(response, batch)

    def _build_request(self, batch: PendingBatch) -> Any:
        snapshots = [
            self._pb2.ReferenceSnapshot(
                symbol=s.symbol,
                mid_price_nanos=s.mid_price_nanos,
                adv_30d_nanos=s.adv_30d_nanos,
                as_of_ns=s.as_of_ns,
                is_stale=s.is_stale,
            )
            for s in batch.snapshots
        ]
        kwargs: dict[str, Any] = {"snapshots": snapshots}
        if batch.regime is not None:
            kwargs["regime"] = self._snapshot_pb2.RegimeLabelPacket(
                label=self._regime_enum_value(batch.regime.label),
                confidence=batch.regime.confidence,
                timestamp_ns=batch.regime.timestamp_ns,
                state_index=batch.regime.state_index,
            )
        return self._pb2.PushReferenceDataRequest(**kwargs)

    def _regime_enum_value(self, label: str) -> int:
        enum_type = self._snapshot_pb2.RegimeLabelPacket.DESCRIPTOR.fields_by_name[
            "label"
        ].enum_type
        return enum_type.values_by_name[label].number

    def _parse_response(self, response: Any, batch: PendingBatch) -> PushResult:
        rejected = [Rejection(key=r.key, reason=r.reason) for r in response.rejected]
        rejected_keys = {r.key for r in rejected}
        applied_symbols = [
            s.symbol for s in batch.snapshots if s.symbol not in rejected_keys
        ]
        regime_applied = bool(response.regime_applied)
        for r in rejected:
            log.warning("reference_data_rejected", key=r.key, reason=r.reason)
        return PushResult(
            applied_snapshot_symbols=applied_symbols,
            regime_applied=regime_applied,
            rejected=rejected,
        )
