"""Write-once manifest store.

``ManifestStore`` is the interface. ``FilesystemManifestStore`` keeps one file per manifest, named
by a gapless
sequence number (``000000000001.json``). Files are created with ``O_CREAT | O_EXCL`` (an existing
file is NEVER
opened for writing, so overwrite is impossible through this API) and then made read-only. Because
the name is the
chain position, two writers racing for the same position cannot both succeed: the loser gets
ManifestStoreError
(chain fork refused). Every successful write is recorded through the injected audit logger.

Limits (be honest): file permissions and O_EXCL stop this code, not a privileged OS user; tamper
*evidence*
comes from ``verify_store`` (digest recomputation + hash chain) plus the audit chain. Production
storage should
additionally be WORM/object-lock media -- TODO(owner). Post-trade metrics are not filled (the
manifest is
immutable); a separate supplement record type would be needed -- proposal, not implemented.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from afe_manifest.audit import AuditSink
from afe_manifest.errors import ManifestAuditError, ManifestStoreError
from afe_manifest.model import GENESIS_HASH, BuiltManifest
from afe_manifest.verify import verify, verify_document

_NAME_RE = re.compile(r"^(\d{12})\.json$")
_O_BINARY = getattr(os, "O_BINARY", 0)


@dataclass(frozen=True)
class StoredManifest:
    seq: int
    manifest_id: str
    digest: str
    path: Path


@dataclass(frozen=True)
class StoreVerification:
    ok: bool
    manifests_checked: int
    problems: tuple[str, ...]


class ManifestStore(Protocol):
    def put(self, manifest: BuiltManifest) -> StoredManifest: ...

    def latest_digest(self) -> str: ...


class FilesystemManifestStore:
    def __init__(
        self,
        root: Path | str,
        audit: AuditSink,
        actor: str,
        audit_event_type: str = "manifest.stored",
    ) -> None:
        if not actor.strip():
            raise ManifestStoreError("actor (identity of the writing service) is required")
        self._root = Path(root)
        self._audit = audit
        self._actor = actor
        self._event_type = audit_event_type
        self._root.mkdir(parents=True, exist_ok=True)

    # -- reads ---------------------------------------------------------------------------------

    def _files(self) -> list[tuple[int, Path]]:
        found = []
        for path in self._root.iterdir():
            match = _NAME_RE.match(path.name)
            if match:
                found.append((int(match.group(1)), path))
        return sorted(found)

    def latest_digest(self) -> str:
        files = self._files()
        if not files:
            return GENESIS_HASH
        report = verify_document(self._read(files[-1][1]))
        if not report.ok:
            raise ManifestStoreError(f"newest stored manifest is corrupt: {report.problems}")
        return str(self._read(files[-1][1])["manifest_digest"])

    def get(self, seq: int) -> dict[str, Any]:
        path = self._path(seq)
        if not path.exists():
            raise ManifestStoreError(f"no manifest at sequence {seq}")
        doc = self._read(path)
        report = verify_document(doc)
        if not report.ok:
            raise ManifestStoreError(f"manifest {seq} failed verification: {report.problems}")
        return doc

    def verify_store(self) -> StoreVerification:
        problems: list[str] = []
        previous = GENESIS_HASH
        files = self._files()
        for index, (seq, path) in enumerate(files, start=1):
            if seq != index:
                problems.append(f"sequence gap: expected {index}, found {seq}")
                break
            try:
                doc = self._read(path)
            except ManifestStoreError as exc:
                problems.append(f"{path.name}: {exc}")
                break
            report = verify_document(doc)
            if not report.ok:
                problems.append(f"{path.name}: {'; '.join(report.problems)}")
                break
            if doc["manifest"]["prev_manifest_hash"] != previous:
                problems.append(f"{path.name}: hash chain broken")
                break
            previous = doc["manifest_digest"]
        return StoreVerification(not problems, len(files), tuple(problems))

    # -- writes --------------------------------------------------------------------------------

    def put(self, manifest: BuiltManifest) -> StoredManifest:
        report = verify(manifest)
        if not report.ok:
            raise ManifestStoreError(f"refusing to store an invalid manifest: {report.problems}")
        head = self.latest_digest()
        if manifest.document["manifest"]["prev_manifest_hash"] != head:
            raise ManifestStoreError("prev_manifest_hash does not match the current chain head")
        seq = len(self._files()) + 1
        path = self._path(seq)
        self._write_exclusive(path, (manifest.document_json + "\n").encode("utf-8"))
        stored = StoredManifest(seq, manifest.manifest_id, manifest.digest, path)
        self._emit_audit(stored, manifest.document)
        return stored

    def reaudit(self, seq: int) -> None:
        """Re-emit the audit record for an already written manifest (after a ManifestAuditError)."""
        doc = self.get(seq)
        manifest_id = doc["manifest"]["manifest_id"]
        stored = StoredManifest(seq, manifest_id, doc["manifest_digest"], self._path(seq))
        self._emit_audit(stored, doc)

    def _emit_audit(self, stored: StoredManifest, data: dict[str, Any]) -> None:
        payload: dict[str, object] = {
            "manifest_id": stored.manifest_id,
            "seq": stored.seq,
            "manifest_digest": stored.digest,
            "prev_manifest_hash": data["manifest"]["prev_manifest_hash"],
            "retain_until": data["retention"]["retain_until"],
            "file": stored.path.name,
        }
        try:
            self._audit.record(self._event_type, self._actor, payload)
        except Exception as exc:  # noqa: BLE001 - re-raised below as ManifestAuditError; the file cannot be undone
            raise ManifestAuditError(
                f"manifest {stored.manifest_id} written at seq {stored.seq} but NOT audited: {exc}",
                stored.seq,
                stored.manifest_id,
            ) from exc

    # -- helpers -------------------------------------------------------------------------------

    def _path(self, seq: int) -> Path:
        return self._root / f"{seq:012d}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ManifestStoreError(f"cannot read manifest file {path.name}: {exc}") from exc
        if not isinstance(doc, dict):
            raise ManifestStoreError(f"{path.name} is not a JSON object")
        return doc

    @staticmethod
    def _write_exclusive(path: Path, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY
        try:
            fd = os.open(path, flags, 0o444)
        except FileExistsError as exc:
            raise ManifestStoreError(
                f"{path.name} already exists: write-once violation or concurrent writer "
                "(chain fork refused)"
            ) from exc
        except OSError as exc:
            raise ManifestStoreError(f"cannot create {path.name}: {exc}") from exc
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ManifestStoreError(f"cannot write {path.name}: {exc}") from exc
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


__all__ = ["FilesystemManifestStore", "ManifestStore", "StoreVerification", "StoredManifest"]
