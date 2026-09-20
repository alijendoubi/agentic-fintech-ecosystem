from __future__ import annotations

from execution_motor.attestation import (
    AttestationVerifier,
    DenyAllVerifier,
    evaluate_attestation,
)
from execution_motor.models import Attestation, AttestedOrder, RejectReason

from .helpers import HmacTestVerifier, attest, make_order


def test_default_verifier_denies_everything() -> None:
    verifier = DenyAllVerifier()
    good = attest(make_order())
    assert verifier.verify(good.attestation.signed_payload, good.attestation.signature, "test-key-1") is False
    assert verifier.verify(b"", b"", "") is False


def test_valid_attestation_passes_with_test_double() -> None:
    assert evaluate_attestation(HmacTestVerifier(), attest(make_order())) is None


def test_default_verifier_rejects_even_a_correctly_signed_order() -> None:
    assert evaluate_attestation(DenyAllVerifier(), attest(make_order())) is RejectReason.ATTESTATION_INVALID


def _with(att: AttestedOrder, **changes: object) -> AttestedOrder:
    return att.model_copy(update={"attestation": att.attestation.model_copy(update=changes)})


def test_missing_signature_or_key_id() -> None:
    good = attest(make_order())
    assert evaluate_attestation(HmacTestVerifier(), _with(good, signature=b"")) is RejectReason.ATTESTATION_MISSING
    assert evaluate_attestation(HmacTestVerifier(), _with(good, key_id="")) is RejectReason.ATTESTATION_MISSING


def test_tampered_signature_or_payload_or_key() -> None:
    good = attest(make_order())
    bad_sig = _with(good, signature=b"\x00" * 32)
    bad_payload = _with(good, signed_payload=good.attestation.signed_payload + b"x")
    bad_key = _with(good, key_id="other-key")
    for tampered in (bad_sig, bad_payload, bad_key):
        assert evaluate_attestation(HmacTestVerifier(), tampered) is RejectReason.ATTESTATION_INVALID


def test_signature_by_wrong_secret_is_invalid() -> None:
    forged = attest(make_order(), secret=b"attacker-secret")
    assert evaluate_attestation(HmacTestVerifier(), forged) is RejectReason.ATTESTATION_INVALID


def test_verifier_exception_is_denial_not_crash() -> None:
    class Boom:
        def verify(self, payload: bytes, signature: bytes, key_id: str) -> bool:
            raise RuntimeError("hsm unreachable")

    verifier: AttestationVerifier = Boom()
    assert evaluate_attestation(verifier, attest(make_order())) is RejectReason.ATTESTATION_INVALID


def test_only_literal_true_counts_as_verified() -> None:
    class Sloppy:
        def verify(self, payload: bytes, signature: bytes, key_id: str) -> object:
            return "yes"

    assert evaluate_attestation(Sloppy(), attest(make_order())) is RejectReason.ATTESTATION_INVALID  # type: ignore[arg-type]


def test_attestation_model_is_frozen_copy_semantics() -> None:
    a = Attestation(signature=b"s", key_id="k", signed_payload=b"p")
    assert a.model_copy(update={"key_id": "z"}).key_id == "z"
    assert a.key_id == "k"
