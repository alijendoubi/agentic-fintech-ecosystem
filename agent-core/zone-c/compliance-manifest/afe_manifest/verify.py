"""Manifest verification: recompute every digest from the document itself."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from afe_manifest.builder import input_sections
from afe_manifest.canonical import canonical_text, digest_of, digest_text, message_to_canonical
from afe_manifest.errors import ManifestError
from afe_manifest.model import INPUT_SECTIONS, SCHEMA_VERSION, BuiltManifest, VerificationReport
from afe_manifest.retention import RETENTION_YEARS, retain_until_ns

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED = (
    "schema_version",
    "manifest",
    "model_versions",
    "input_digests",
    "retention",
    "manifest_digest",
)


def _structure_problems(doc: Mapping[str, Any]) -> list[str]:
    problems = [f"missing key: {key}" for key in _REQUIRED if key not in doc]
    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"unsupported schema_version: {doc.get('schema_version')!r}")
    return problems


def _retention_problems(doc: Mapping[str, Any]) -> list[str]:
    ret, manifest = doc["retention"], doc["manifest"]
    problems: list[str] = []
    try:
        years, created = int(ret["years"]), int(ret["created_at_ns"])
        if years < RETENTION_YEARS:
            problems.append(f"retention years {years} below the required {RETENTION_YEARS}")
        if created != manifest["created_at_ns"]:
            problems.append("retention.created_at_ns differs from manifest.created_at_ns")
        if ret["retain_until_ns"] != retain_until_ns(created, years):
            problems.append("retention.retain_until_ns is not created_at + years")
    except (KeyError, TypeError, ValueError):
        problems.append("retention block is malformed")
    return problems


def _digest_problems(doc: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    try:
        sections = input_sections(doc["manifest"], doc["model_versions"])
    except (KeyError, TypeError):
        return ["manifest block is malformed"]
    recorded = doc["input_digests"]
    for name in INPUT_SECTIONS:
        if recorded.get(name) != digest_of(sections[name]):
            problems.append(f"input digest mismatch: {name}")
    body = {k: v for k, v in doc.items() if k != "manifest_digest"}
    if doc["manifest_digest"] != digest_text(canonical_text(body)):
        problems.append("manifest_digest mismatch")
    if not _HASH_RE.match(str(doc["manifest"].get("prev_manifest_hash", ""))):
        problems.append("prev_manifest_hash is not a sha256 hex string")
    return problems


def verify_document(doc: Mapping[str, Any]) -> VerificationReport:
    """Recompute input digests, the manifest digest and retention arithmetic from the envelope
    alone."""
    problems = _structure_problems(doc)
    if not problems:
        problems += _digest_problems(doc)
        problems += _retention_problems(doc)
    return VerificationReport(ok=not problems, problems=tuple(problems))


def verify(manifest: BuiltManifest) -> VerificationReport:
    """Full check of a BuiltManifest: document digests, canonical text form, and protobuf/document
    agreement."""
    doc = manifest.document
    report = verify_document(doc)
    problems = list(report.problems)
    if manifest.document_json != canonical_text(doc):
        problems.append("document_json is not in canonical form")
    if doc.get("manifest_digest") != manifest.digest:
        problems.append("digest attribute differs from manifest_digest in the document")
    try:
        if message_to_canonical(manifest.proto) != doc.get("manifest"):
            problems.append("protobuf message differs from the manifest section of the document")
    except ManifestError as exc:
        problems.append(f"protobuf message cannot be canonicalised: {exc}")
    return VerificationReport(ok=not problems, problems=tuple(problems))
