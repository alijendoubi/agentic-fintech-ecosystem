"""Value objects for the manifest pipeline."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

GENESIS_HASH = "0" * 64
SCHEMA_VERSION = 1
# Document sections whose digests are recorded in ``input_digests``.
INPUT_SECTIONS = (
    "snapshot",
    "signal",
    "order",
    "ptc_checks",
    "hitl_override",
    "cognitive_outputs",
    "model_versions",
)


@dataclass(frozen=True)
class CognitiveOutputs:
    """Free-text Cognitive Core outputs that the proto carries verbatim."""

    blue_node_thesis: str = ""
    red_node_challenge: str = ""
    judge_synthesis: str = ""
    compression_summaries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestInputs:
    """Everything needed to assemble one per-trade manifest. Protobuf messages come from the real
    stubs
    (market_snapshot_pb2.MarketSnapshot, trade_signal_pb2.TradeSignal,
    order_request_pb2.OrderRequest,
    compliance_manifest_pb2.PTCCheckResult / HITLOverrideRecord)."""

    snapshot: Any
    signal: Any
    order: Any | None
    model_versions: Mapping[str, str]
    ptc_checks: Sequence[Any]
    prev_manifest_hash: str
    hitl_override: Any | None = None
    cognitive: CognitiveOutputs = field(default_factory=CognitiveOutputs)
    mar_flag_triggered: bool = False
    mar_flag_reason: str = ""


@dataclass(frozen=True)
class BuiltManifest:
    """An assembled manifest. ``document_json`` (canonical text) is the source of truth; ``proto``
    is the
    ComplianceManifest message built from the same inputs (kept for consumers of the protobuf
    contract)."""

    manifest_id: str
    digest: str
    document_json: str
    proto: Any = field(compare=False, repr=False)

    @property
    def document(self) -> dict[str, Any]:
        doc: dict[str, Any] = json.loads(self.document_json)
        return doc


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    problems: tuple[str, ...]
