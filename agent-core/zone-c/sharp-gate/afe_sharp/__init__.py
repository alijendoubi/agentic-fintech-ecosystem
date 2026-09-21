"""SHARP rubric-promotion gate (ALI-56): enforced state machine over
docs/processes/sharp-promotion.md."""

from afe_sharp.errors import (
    AbortNotRecordedError,
    AuditFailureError,
    ConcurrencyError,
    DistinctApproverError,
    DuplicateProposalError,
    NotAuthorizedError,
    ProposalClosedError,
    ProposalValidationError,
    SelfApprovalError,
    SharpError,
    StageSkipError,
    StoreError,
    UnknownProposalError,
)
from afe_sharp.gate import SharpGate
from afe_sharp.memory_store import InMemoryProposalStore
from afe_sharp.models import (
    PIPELINE,
    Approval,
    AuditEntry,
    AuditLookup,
    ProposalRecord,
    RubricChangeProposal,
    Stage,
    TransitionEvent,
    normalise_identity,
)
from afe_sharp.pg_audit import PostgresAuditLookup
from afe_sharp.pg_store import PostgresProposalStore
from afe_sharp.ports import (
    ApproverAuthorizer,
    AuditReceipt,
    AuditSink,
    ProposalStore,
    StaticRoleAuthorizer,
)

__all__ = [
    "PIPELINE",
    "AbortNotRecordedError",
    "Approval",
    "ApproverAuthorizer",
    "AuditEntry",
    "AuditFailureError",
    "AuditLookup",
    "AuditReceipt",
    "AuditSink",
    "ConcurrencyError",
    "DistinctApproverError",
    "DuplicateProposalError",
    "InMemoryProposalStore",
    "NotAuthorizedError",
    "PostgresAuditLookup",
    "PostgresProposalStore",
    "ProposalClosedError",
    "ProposalRecord",
    "ProposalStore",
    "ProposalValidationError",
    "RubricChangeProposal",
    "SelfApprovalError",
    "SharpError",
    "SharpGate",
    "Stage",
    "StageSkipError",
    "StaticRoleAuthorizer",
    "StoreError",
    "TransitionEvent",
    "UnknownProposalError",
    "normalise_identity",
]
