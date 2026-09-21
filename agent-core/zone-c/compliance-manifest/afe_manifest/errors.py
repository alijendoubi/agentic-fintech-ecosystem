"""Exceptions. Any ManifestError means: NO valid, stored, audited manifest exists for this trade;
the caller must not proceed to order release (fail closed)."""

from __future__ import annotations


class ManifestError(Exception):
    """Base class."""


class ManifestBuildError(ManifestError):
    """Inputs are missing, inconsistent or unsafe; nothing was built."""


class ManifestStoreError(ManifestError):
    """A write-once/chain rule was violated or the filesystem failed; nothing new was committed."""


class ManifestAuditError(ManifestError):
    """The manifest file was written (write-once, cannot be undone) but recording it in the audit
    log failed.

    The trade must not proceed. ``FilesystemManifestStore.reaudit(seq)`` re-emits the audit
    record."""

    def __init__(self, message: str, seq: int, manifest_id: str) -> None:
        super().__init__(message)
        self.seq = seq
        self.manifest_id = manifest_id
