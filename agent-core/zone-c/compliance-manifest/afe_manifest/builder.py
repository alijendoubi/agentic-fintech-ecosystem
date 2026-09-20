"""Per-trade Compliance Manifest builder (ALI-53).

The protobuf ``ComplianceManifest`` has no fields for model/version identifiers, input digests,
retention or its own
digest, so the stored artifact is a canonical JSON *envelope*:

    {schema_version, manifest: <ComplianceManifest, canonical>, model_versions, input_digests,
    retention,
     manifest_digest}

``input_digests`` are sha256 digests of the canonical form of each input section (snapshot, signal,
order,
ptc_checks, hitl_override, cognitive_outputs, model_versions); ``manifest_digest`` is sha256 of the
whole envelope
without that field and is what the next manifest carries as ``prev_manifest_hash`` (hash chain).
Every inconsistency raises ManifestBuildError: no partial manifest is ever returned (fail closed).
"""

from __future__ import annotations

import math
import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from afe_manifest import stubs
from afe_manifest.canonical import canonical_text, digest_of, digest_text, message_to_canonical
from afe_manifest.errors import ManifestBuildError
from afe_manifest.model import (
    GENESIS_HASH,
    INPUT_SECTIONS,
    SCHEMA_VERSION,
    BuiltManifest,
    ManifestInputs,
)
from afe_manifest.retention import (
    RETENTION_SOURCES,
    RETENTION_YEARS,
    format_ns_utc,
    retain_until_ns,
)

MAX_COMPRESSION_SUMMARIES = 3  # proto: "Last 3 debate summaries used"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_HITL_DECISIONS = ("approved", "rejected")


def _expect(message: Any, full_name: str, label: str) -> None:
    descriptor = getattr(message, "DESCRIPTOR", None)
    if descriptor is None or descriptor.full_name != full_name:
        raise ManifestBuildError(f"{label} must be a {full_name} message")


def _check_common(inputs: ManifestInputs) -> None:
    _expect(inputs.snapshot, "afe.shared.MarketSnapshot", "snapshot")
    _expect(inputs.signal, "afe.shared.TradeSignal", "signal")
    if inputs.order is not None:
        _expect(inputs.order, "afe.shared.OrderRequest", "order")
    if not _HASH_RE.match(inputs.prev_manifest_hash):
        raise ManifestBuildError(
            "prev_manifest_hash must be 64 lowercase hex chars (genesis: zeros)"
        )
    if not inputs.model_versions:
        raise ManifestBuildError("model_versions must identify at least one model/version")
    for name, version in inputs.model_versions.items():
        if not name.strip() or not version.strip():
            raise ManifestBuildError("model_versions keys and values must be non-empty")
    if len(inputs.cognitive.compression_summaries) > MAX_COMPRESSION_SUMMARIES:
        raise ManifestBuildError(f"at most {MAX_COMPRESSION_SUMMARIES} compression summaries")


def _check_ptc(inputs: ManifestInputs) -> tuple[bool, bool, str]:
    """Validate pre-trade-control results; returns (hard_block, soft_block, soft_reason)."""
    if not inputs.ptc_checks:
        raise ManifestBuildError("pre-trade-control results are required (none supplied)")
    cm = stubs.load("compliance_manifest_pb2")
    hard = soft = False
    soft_reasons: list[str] = []
    for check in inputs.ptc_checks:
        _expect(check, "afe.shared.PTCCheckResult", "ptc check")
        if not check.check_name.strip():
            raise ManifestBuildError("every PTC check needs a check_name")
        if check.ptc_type not in (cm.HARD_BLOCK, cm.SOFT_BLOCK):
            raise ManifestBuildError(f"PTC check {check.check_name!r} has no HARD/SOFT type")
        if not (math.isfinite(check.threshold_value) and math.isfinite(check.actual_value)):
            raise ManifestBuildError(f"PTC check {check.check_name!r} has non-finite values")
        if not check.passed:
            if check.ptc_type == cm.HARD_BLOCK:
                hard = True
            else:
                soft = True
                soft_reasons.append(check.reason or check.check_name)
    return hard, soft, "; ".join(soft_reasons)


def _check_consistency(inputs: ManifestInputs, hard: bool, soft: bool) -> None:
    snap, signal, order, hitl = inputs.snapshot, inputs.signal, inputs.order, inputs.hitl_override
    if snap.symbol != signal.symbol or not snap.symbol:
        raise ManifestBuildError("snapshot and signal must reference the same non-empty symbol")
    if hitl is not None:
        _expect(hitl, "afe.shared.HITLOverrideRecord", "hitl_override")
        if hitl.decision not in _HITL_DECISIONS or not hitl.operator_id.strip():
            raise ManifestBuildError("HITL record needs operator_id and decision approved|rejected")
    if order is None:
        return
    if order.signal_id != signal.signal_id or order.symbol != signal.symbol:
        raise ManifestBuildError("order does not belong to the signal (signal_id/symbol mismatch)")
    if hard:
        raise ManifestBuildError("a hard-block PTC failed: an order must not exist for this trade")
    if snap.is_stale:
        raise ManifestBuildError("order was derived from a stale market snapshot")
    if hitl is not None and hitl.decision == "rejected":
        raise ManifestBuildError("HITL rejected the trade: an order must not exist")
    if soft and (hitl is None or hitl.decision != "approved"):
        raise ManifestBuildError(
            "soft-block PTC failed: an approved HITL record is required for an order"
        )


def _assemble_proto(
    inputs: ManifestInputs,
    manifest_id: str,
    created_at_ns: int,
    hard: bool,
    soft: bool,
    reason: str,
) -> Any:
    cm = stubs.load("compliance_manifest_pb2")
    cog, signal = inputs.cognitive, inputs.signal
    msg = cm.ComplianceManifest(
        manifest_id=manifest_id,
        prev_manifest_hash=inputs.prev_manifest_hash,
        created_at_ns=created_at_ns,
        omega=signal.omega,
        expected_value=signal.expected_value,
        blue_node_thesis=cog.blue_node_thesis,
        red_node_challenge=cog.red_node_challenge,
        judge_synthesis=cog.judge_synthesis or signal.debate_summary,
        compression_summaries=list(cog.compression_summaries),
        hard_block_triggered=hard,
        soft_block_triggered=soft,
        soft_block_reason=reason,
        hitl_override_occurred=inputs.hitl_override is not None,
        mar_flag_triggered=inputs.mar_flag_triggered,
        mar_flag_reason=inputs.mar_flag_reason,
    )
    msg.snapshot.CopyFrom(inputs.snapshot)
    msg.signal.CopyFrom(signal)
    msg.ptc_checks.extend(inputs.ptc_checks)
    if inputs.order is not None:
        msg.order.CopyFrom(inputs.order)
    if inputs.hitl_override is not None:
        msg.hitl_override.CopyFrom(inputs.hitl_override)
    return msg


def input_sections(
    proto_doc: Mapping[str, Any], model_versions: Mapping[str, Any]
) -> dict[str, Any]:
    """The sections that are digested. Shared with verification so both sides derive them
    identically."""
    return {
        "snapshot": proto_doc["snapshot"],
        "signal": proto_doc["signal"],
        "order": proto_doc["order"],
        "ptc_checks": proto_doc["ptc_checks"],
        "hitl_override": proto_doc["hitl_override"],
        "cognitive_outputs": {
            key: proto_doc[key]
            for key in (
                "blue_node_thesis",
                "red_node_challenge",
                "judge_synthesis",
                "compression_summaries",
                "omega",
                "expected_value",
            )
        },
        "model_versions": dict(model_versions),
    }


def build_manifest(
    inputs: ManifestInputs,
    *,
    clock_ns: Callable[[], int] = time.time_ns,
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    retention_years: int = RETENTION_YEARS,
) -> BuiltManifest:
    if retention_years < RETENTION_YEARS:
        raise ManifestBuildError(f"retention_years must be >= {RETENTION_YEARS}")
    _check_common(inputs)
    hard, soft, reason = _check_ptc(inputs)
    _check_consistency(inputs, hard, soft)
    created_at_ns = clock_ns()
    if created_at_ns <= 0:
        raise ManifestBuildError("clock returned a non-positive timestamp")
    manifest_id = id_factory()
    if not manifest_id.strip():
        raise ManifestBuildError("manifest_id must be non-empty")
    proto = _assemble_proto(inputs, manifest_id, created_at_ns, hard, soft, reason)
    proto_doc = message_to_canonical(proto)
    sections = input_sections(proto_doc, inputs.model_versions)
    until = retain_until_ns(created_at_ns, retention_years)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "manifest": proto_doc,
        "model_versions": dict(inputs.model_versions),
        "input_digests": {name: digest_of(sections[name]) for name in INPUT_SECTIONS},
        "retention": {
            "years": retention_years,
            "created_at_ns": created_at_ns,
            "retain_until_ns": until,
            "retain_until": format_ns_utc(until),
            "basis": list(RETENTION_SOURCES),
        },
    }
    digest = digest_text(canonical_text(document))
    document["manifest_digest"] = digest
    return BuiltManifest(
        manifest_id=manifest_id, digest=digest, document_json=canonical_text(document), proto=proto
    )


__all__ = ["GENESIS_HASH", "build_manifest", "input_sections"]
