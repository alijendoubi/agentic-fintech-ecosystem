"""Aegis attestation public-key registry, loaded from a JSON file.

The motor is a CONSUMER of Aegis's signatures: it must know Aegis's public verification
keys out of band, never trust a caller-supplied key. File shape (array, non-empty)::

    [
      {"key_id": "dev-1", "algorithm": "ED25519", "public_key_hex": "..."},
      {"key_id": "hsm-1", "algorithm": "ECDSA_P256_SHA256", "public_key_hex": "..."}
    ]

``algorithm`` matches ``execution_motor.verifiers.SignatureAlgorithm`` values. Production
refusal of dev keys is enforced by ``AegisAttestationVerifier`` itself (and again by
``ExecutionMotor.__init__``); this loader only parses the file.
"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import ConfigError
from .verifiers import RegisteredKey, SignatureAlgorithm

_ALGORITHMS = {a.value: a for a in SignatureAlgorithm}


def load_attestation_keys(path: Path) -> list[RegisteredKey]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"attestation keys file not readable: {path}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"attestation keys file is not valid JSON: {path}") from exc
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"attestation keys file must be a non-empty JSON array: {path}")
    keys: list[RegisteredKey] = []
    for index, entry in enumerate(raw):
        try:
            key_id = str(entry["key_id"])
            algorithm = _ALGORITHMS[str(entry["algorithm"])]
            public_key = bytes.fromhex(str(entry["public_key_hex"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise ConfigError(f"attestation keys file entry {index} is malformed: {exc}") from exc
        keys.append(RegisteredKey(key_id=key_id, algorithm=algorithm, public_key=public_key))
    return keys
