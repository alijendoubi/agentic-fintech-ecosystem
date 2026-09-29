"""OIDC-attested hold approvals for Aegis (owner decision 2026-09-29, DECISIONS row 4).

Aegis (ALI-164) accepts a second approval only as a signed ``afe-hold-v1`` ``Authorization``. The
owner chose "a distinct authenticated OIDC subject per approval" instead of per-person keys, so
this service attests each approval it has authenticated: it signs the same canonical text with an
Ed25519 key that Aegis lists under ``hold_attestors`` in its identities file, bound to this
service's mTLS identity, with ``approver_id`` = the operator's JWT ``sub`` and
``credential_ref = "oidc-attested:<attestor_id>:<hex signature>"``.

Trust model (say it plainly): this trusts hitl-backend (which holds the key and re-verifies every
operator token) and the token issuer. It is NOT a key held by each person; a compromised
hitl-backend host, attestor key or IdP can approve as anyone. TODO(owner): whether that is
acceptable for production, or per-person hardware keys are required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .config import ConfigError

ATTESTED_PREFIX = "oidc-attested:"
APPROVAL_ROLE = "operator"  # Aegis requires the operator role on hold approvals
MAX_SUBJECT = 128  # Aegis refuses longer attested subjects
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ATTESTOR_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class SubjectNotAttestable(ValueError):
    """The JWT subject cannot be put into the line-based canonical text."""


def hold_approval_text(
    hold_id: str, approve: bool, approver_id: str, role: str, approved_at_ns: int
) -> str:
    """Byte-for-byte the Aegis ``identity::hold_approval_text``."""
    return (
        f"afe-hold-v1\nhold_id={hold_id}\napprove={'true' if approve else 'false'}\n"
        f"approver_id={approver_id}\nrole={role}\napproved_at_ns={approved_at_ns}\n"
    )


@dataclass(frozen=True)
class Attestation:
    """The fields of an ``aegis.proto`` ``Authorization``."""

    approver_id: str
    role: str
    approved_at_ns: int
    credential_ref: str

    def as_fields(self) -> dict[str, object]:
        return {
            "approver_id": self.approver_id,
            "role": self.role,
            "approved_at_ns": self.approved_at_ns,
            "credential_ref": self.credential_ref,
        }


class ApprovalAttestor:
    def __init__(self, attestor_id: str, key: Ed25519PrivateKey, *, issuer: str | None) -> None:
        if _ATTESTOR_ID.fullmatch(attestor_id) is None:
            raise ConfigError("HITL_APPROVAL_ATTESTOR_ID must be 1-64 chars of [A-Za-z0-9._-]")
        self.attestor_id = attestor_id
        self.issuer = issuer
        self._key = key

    @classmethod
    def from_seed_file(
        cls, attestor_id: str, path: Path, *, issuer: str | None
    ) -> ApprovalAttestor:
        """The key file holds the 32-byte Ed25519 seed as 64 hex characters (the format of the
        dev approver seeds, ``dev-tls/generate-dev-certs.sh``)."""
        try:
            seed = bytes.fromhex(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise ConfigError(f"cannot read the attestor key: {type(exc).__name__}") from exc
        if len(seed) != 32:
            raise ConfigError("the attestor key must be a 32-byte Ed25519 seed (64 hex chars)")
        return cls(attestor_id, Ed25519PrivateKey.from_private_bytes(seed), issuer=issuer)

    def public_key_hex(self) -> str:
        return self._key.public_key().public_bytes_raw().hex()

    def attest(self, hold_id: str, approve: bool, subject: str, at_ns: int) -> Attestation:
        if not subject.strip() or len(subject) > MAX_SUBJECT or _CONTROL.search(subject):
            raise SubjectNotAttestable("the token subject cannot be attested to Aegis")
        text = hold_approval_text(hold_id, approve, subject, APPROVAL_ROLE, at_ns)
        signature = self._key.sign(text.encode("utf-8")).hex()
        return Attestation(
            approver_id=subject,
            role=APPROVAL_ROLE,
            approved_at_ns=at_ns,
            credential_ref=f"{ATTESTED_PREFIX}{self.attestor_id}:{signature}",
        )
