"""Exceptions. Every one of them means: the transition did NOT happen (or is not audited) and
nothing may be
deployed on the strength of it (fail closed)."""

from __future__ import annotations


class SharpError(Exception):
    """Base class."""


class ProposalValidationError(SharpError):
    """The proposal or a request field is invalid."""


class UnknownProposalError(SharpError):
    """No such proposal."""


class DuplicateProposalError(SharpError):
    """A proposal with this id already exists (proposals are immutable)."""


class ProposalClosedError(SharpError):
    """The proposal is PROMOTED or REJECTED; no further transition is possible (no approval after
    rejection)."""


class StageSkipError(SharpError):
    """The requested gate is not the next stage in the SHARP sequence."""


class SelfApprovalError(SharpError):
    """The proposer may not approve their own proposal."""


class DistinctApproverError(SharpError):
    """This identity already signed off another stage of the same proposal."""


class NotAuthorizedError(SharpError):
    """The authorizer does not allow this identity to act on this stage."""


class ConcurrencyError(SharpError):
    """Another transition was recorded first; reload and retry deliberately."""


class StoreError(SharpError):
    """The store failed or its history is inconsistent (corrupted/tampered)."""


class AuditFailureError(SharpError):
    """The audit logger refused the record; the transition was NOT performed."""
