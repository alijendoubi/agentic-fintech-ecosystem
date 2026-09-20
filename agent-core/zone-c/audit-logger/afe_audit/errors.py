"""Exception hierarchy. Every audit failure is an exception: callers must treat any AuditError as
"the action was NOT audited and MUST NOT proceed" (fail closed)."""

from __future__ import annotations


class AuditError(Exception):
    """Base class for all audit-logger failures."""


class AuditValidationError(AuditError):
    """The event cannot be canonicalised (bad type, float, NUL, too deep, ...). Nothing was
    written."""


class AuditWriteError(AuditError):
    """The database write failed or its outcome is unknown. Treat as NOT audited; the caller must
    not proceed.

    If the connection was lost while COMMIT was in flight the row may or may not exist; the chain
    stays valid
    either way, and the caller must not retry blindly (use ChainVerifier to reconcile)."""


class AnchorError(AuditError):
    """An anchor sink failed to emit/read, or an anchor file is malformed/tampered."""
