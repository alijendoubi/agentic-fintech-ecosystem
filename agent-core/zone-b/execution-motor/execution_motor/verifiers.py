"""Real Aegis attestation verifier: Ed25519 (dev) and ECDSA-P256 (HSM), selected by ``key_id``.

Aegis attestations carry no algorithm field (frozen proto), so the algorithm comes from the
motor's key registry. What is signed (aegis/src/signing, README "Attestation contract"):

* ``digest = SHA-256(canonical text)``; the DEV Ed25519 signer signs those 32 bytes, so the
  verifier hashes the payload and verifies the signature over the DIGEST (not the text).
* the HSM signs the same digest with ECDSA P-256 (raw ``r || s``, 64 bytes), which is exactly
  what a standard ECDSA-SHA256 verification of the TEXT checks, so it is verified over the text.

The dev key is refused in production: registering one raises ConfigError, and the motor also
refuses a verifier that ``accepts_dev_keys`` when its config is production.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import structlog
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .errors import ConfigError

_log = structlog.get_logger("execution_motor.verifiers")

_DEV_KEY_PREFIX: Final = "dev-"
_ED25519_SIG_LEN: Final = 64
_P256_RAW_SIG_LEN: Final = 64
_P256_COORD_LEN: Final = 32


class SignatureAlgorithm(StrEnum):
    ED25519_DEV = "ED25519"  # DEV ONLY: signature over the 32-byte payload digest
    ECDSA_P256_SHA256 = "ECDSA_P256_SHA256"  # HSM: raw r||s, verified over the text


@dataclass(frozen=True)
class RegisteredKey:
    key_id: str
    algorithm: SignatureAlgorithm
    public_key: bytes  # Ed25519: 32 raw bytes. P-256: 65-byte SEC1 uncompressed point.

    @property
    def is_dev(self) -> bool:
        return self.algorithm is SignatureAlgorithm.ED25519_DEV or self.key_id.startswith(
            _DEV_KEY_PREFIX
        )


def _parse_public_key(key: RegisteredKey) -> Ed25519PublicKey | ec.EllipticCurvePublicKey:
    try:
        if key.algorithm is SignatureAlgorithm.ED25519_DEV:
            return Ed25519PublicKey.from_public_bytes(key.public_key)
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key.public_key)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"public key for {key.key_id!r} is invalid") from exc


class AegisAttestationVerifier:
    """Implements ``AttestationVerifier``. ``verify`` never raises: any failure is ``False``."""

    def __init__(self, keys: Iterable[RegisteredKey], *, production: bool = True) -> None:
        self._parsed: dict[
            str, tuple[SignatureAlgorithm, Ed25519PublicKey | ec.EllipticCurvePublicKey]
        ] = {}
        for key in keys:
            if not key.key_id or key.key_id in self._parsed:
                raise ConfigError("key ids must be non-empty and unique")
            if production and key.is_dev:
                raise ConfigError(f"dev key {key.key_id!r} is forbidden in production")
            self._parsed[key.key_id] = (key.algorithm, _parse_public_key(key))
        if not self._parsed:
            raise ConfigError("at least one attestation key is required")
        self._accepts_dev = any(
            algo is SignatureAlgorithm.ED25519_DEV or kid.startswith(_DEV_KEY_PREFIX)
            for kid, (algo, _) in self._parsed.items()
        )

    @property
    def accepts_dev_keys(self) -> bool:
        return self._accepts_dev

    def verify(self, payload: bytes, signature: bytes, key_id: str) -> bool:
        entry = self._parsed.get(key_id)
        if entry is None:
            return False
        algorithm, public = entry
        try:
            if algorithm is SignatureAlgorithm.ED25519_DEV and isinstance(public, Ed25519PublicKey):
                return self._verify_ed25519(public, payload, signature)
            if isinstance(public, ec.EllipticCurvePublicKey):
                return self._verify_p256(public, payload, signature)
        except InvalidSignature:
            return False
        except (ValueError, TypeError) as exc:
            _log.warning("attestation_signature_malformed", key_id=key_id, error=type(exc).__name__)
        return False

    @staticmethod
    def _verify_ed25519(public: Ed25519PublicKey, payload: bytes, signature: bytes) -> bool:
        if len(signature) != _ED25519_SIG_LEN:
            return False
        public.verify(signature, hashlib.sha256(payload).digest())
        return True

    @staticmethod
    def _verify_p256(public: ec.EllipticCurvePublicKey, payload: bytes, signature: bytes) -> bool:
        if len(signature) != _P256_RAW_SIG_LEN:
            return False
        r = int.from_bytes(signature[:_P256_COORD_LEN], "big")
        s = int.from_bytes(signature[_P256_COORD_LEN:], "big")
        public.verify(encode_dss_signature(r, s), payload, ec.ECDSA(hashes.SHA256()))
        return True
