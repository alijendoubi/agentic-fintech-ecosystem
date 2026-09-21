# compliance-manifest (Zone C) — per-trade manifest pipeline (ALI-53)

Assembles, digests, verifies and write-once-stores one manifest per trade decision. Nothing here is a verified
regulatory-compliance claim (needs qualified legal review).

## Flow

```python
from afe_manifest import ManifestInputs, CognitiveOutputs, FilesystemManifestStore, build_manifest

store = FilesystemManifestStore(root, audit_logger, actor="<identity of this service>")
built = build_manifest(ManifestInputs(
    snapshot=..., signal=..., order=...,                 # real protobuf messages (MarketSnapshot, TradeSignal, OrderRequest)
    model_versions={"<component>": "<version id>"},      # caller-supplied identifiers, none invented here
    ptc_checks=[...],                                    # compliance_manifest_pb2.PTCCheckResult, required
    hitl_override=...,                                   # HITLOverrideRecord, required for an order after a soft block
    prev_manifest_hash=store.latest_digest(),
))
store.put(built)   # any ManifestError => no valid stored+audited manifest => do NOT release the order
```

## What is enforced (fail closed)

* Build refuses: missing/invalid PTC results, no model versions, symbol/signal-id mismatches, an order after a failed
  hard block, an order from a stale snapshot, a soft-block order without an *approved* HITL record, a HITL
  rejection with an order, retention below 7 years.
* Canonical encoding (`afe_manifest/canonical.py`): schema-driven from the protobuf descriptors (not wire bytes),
  doubles as shortest round-trip strings, enums by name, bytes as hex, sorted-key ASCII JSON, sha256.
* Envelope: `{schema_version, manifest, model_versions, input_digests, retention, manifest_digest}`. The proto
  `ComplianceManifest` has no fields for model versions, digests or retention, hence the envelope (see
  "needs from other packages" in the PKG-Z report). `manifest_digest` of manifest *n* is the `prev_manifest_hash`
  of manifest *n+1*.
* Retention: `RETENTION_YEARS = 7` (`afe_manifest/retention.py`, sources cited there: README, the proto comment,
  the EU AI Act Art. 12 row, docker-compose). Calendar-aware; a Feb-29 start maps to Mar 1 (never shorter).
  TODO(owner): legal confirmation of the period.
* `verify(built)` / `verify_document(doc)` recompute every input digest, the manifest digest, retention arithmetic
  and (for `verify`) protobuf/document agreement. `FilesystemManifestStore.verify_store()` re-verifies the whole
  chain and sequence.
* Store: `O_CREAT|O_EXCL`, read-only files, gapless sequence file names (racing writers cannot both win => no
  fork), and one `manifest.stored` audit record per write. If the audit write fails after the (irreversible) file
  write, `ManifestAuditError` is raised (caller must not proceed); `reaudit(seq)` re-emits the record.

## Limits

Filesystem permissions/O_EXCL do not stop a privileged OS user — tamper *evidence* comes from `verify_store` plus
the audit chain; production storage should be WORM/object-lock (TODO(owner)). Post-trade metrics are not filled
(manifests are immutable); a supplement record type would be needed (proposal, not implemented).

## Tests

```bash
cd agent-core/zone-c/compliance-manifest
python -m pytest tests --cov=afe_manifest     # protos compiled from agent-core/shared/proto at test time
python -m ruff check --config ../../../ruff.toml . && python -m mypy
```
`tests/test_integration_audit.py` needs Docker and the sibling `audit-logger` package (test-time only).
