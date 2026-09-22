from __future__ import annotations

import json
from pathlib import Path

import pytest

from execution_motor.errors import ConfigError
from execution_motor.keys import load_attestation_keys
from execution_motor.verifiers import SignatureAlgorithm


def write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "keys.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loads_valid_keys(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        [
            {"key_id": "dev-1", "algorithm": "ED25519", "public_key_hex": "ab" * 32},
            {"key_id": "hsm-1", "algorithm": "ECDSA_P256_SHA256", "public_key_hex": "cd" * 65},
        ],
    )
    keys = load_attestation_keys(path)
    assert len(keys) == 2
    assert keys[0].key_id == "dev-1"
    assert keys[0].algorithm is SignatureAlgorithm.ED25519_DEV
    assert keys[0].public_key == bytes.fromhex("ab" * 32)


def test_missing_file_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_attestation_keys(tmp_path / "missing.json")


def test_empty_list_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_attestation_keys(write(tmp_path, []))


def test_not_a_list_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_attestation_keys(write(tmp_path, {"key_id": "x"}))


def test_invalid_json_refuses(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_attestation_keys(path)


def test_unknown_algorithm_refuses(tmp_path: Path) -> None:
    path = write(tmp_path, [{"key_id": "k", "algorithm": "RSA", "public_key_hex": "ab"}])
    with pytest.raises(ConfigError):
        load_attestation_keys(path)


def test_bad_hex_refuses(tmp_path: Path) -> None:
    path = write(tmp_path, [{"key_id": "k", "algorithm": "ED25519", "public_key_hex": "zz"}])
    with pytest.raises(ConfigError):
        load_attestation_keys(path)


def test_missing_field_refuses(tmp_path: Path) -> None:
    path = write(tmp_path, [{"key_id": "k", "algorithm": "ED25519"}])
    with pytest.raises(ConfigError):
        load_attestation_keys(path)
