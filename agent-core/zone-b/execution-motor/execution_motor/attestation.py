"""Aegis attestation gate. Default deny.

Aegis signs each OrderRequest with an HSM key (ADR-001). The motor verifies through a
pluggable ``AttestationVerifier``. The shipped default, ``DenyAllVerifier``, refuses
everything, so an unconfigured motor executes nothing. A real verifier (ECDSA public-key
verification against the key registry for ``hsm_key_id``) is wired in by a later package;
the HMAC double used in tests lives under tests/ and is not importable from this package.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Protocol

import structlog

from .models import AttestedOrder, RejectReason

_log = structlog.get_logger("execution_motor.attestation")


class AttestationVerifier(Protocol):
    def verify(self, payload: bytes, signature: bytes, key_id: str) -> bool:
        """True iff ``signature`` is a valid Aegis signature over ``payload`` under ``key_id``."""
        ...


class DenyAllVerifier:
    """Default verifier: denies every attestation."""

    def verify(self, payload: bytes, signature: bytes, key_id: str) -> bool:
        return False


def evaluate_attestation(
    verifier: AttestationVerifier, attested: AttestedOrder
) -> RejectReason | None:
    """None when the attestation verifies; otherwise the reject reason. Never raises."""
    att = attested.attestation
    if not att.signature or not att.key_id or not att.signed_payload:
        return RejectReason.ATTESTATION_MISSING
    digest = hashlib.sha256(att.signed_payload).digest()
    if not hmac.compare_digest(digest, att.payload_sha256):
        return RejectReason.ATTESTATION_INVALID
    try:
        verdict = verifier.verify(att.signed_payload, att.signature, att.key_id)
    except Exception as exc:  # noqa: BLE001 - a verifier failure is a denial (fail closed)
        _log.error(
            "attestation_verifier_error",
            order_id=attested.order.order_id,
            error_type=type(exc).__name__,
        )
        return RejectReason.ATTESTATION_INVALID
    # Only the literal True counts; truthy junk from a buggy verifier must not pass.
    if verdict is not True:
        return RejectReason.ATTESTATION_INVALID
    return None
