"""Explicit error types. Callers must tell "store failed" apart from "no precedent found".

A reachable store with no matching records returns an empty list. Anything else (network
failure, timeout, malformed store reply) raises `MemoryUnavailableError`, which callers
must treat as "unknown", never as "no precedent".
"""

from __future__ import annotations


class VectorMemoryError(Exception):
    """Base class for every error raised by this package."""


class MemoryConfigError(VectorMemoryError, ValueError):
    """An environment value is unparsable or unsafe. The process must refuse to start."""


class MemoryValidationError(VectorMemoryError, ValueError):
    """A record or query argument is invalid (caller bug or untrusted input)."""


class EmbeddingError(VectorMemoryError):
    """The embedding function returned unusable vectors (empty, non-finite, wrong shape)."""


class MemoryUnavailableError(VectorMemoryError):
    """The store is unreachable, timed out, or returned a malformed reply.

    Fail-closed contract: never convert this into an empty result.
    """
