"""Dev Ed25519 (over the digest) and HSM ECDSA-P256 (over the text) verification by key_id."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from types import ModuleType

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    Prehashed,
    decode_dss_signature,
)

from execution_motor.attestation import evaluate_attestation
from execution_motor.canonical import build_canonical_text
from execution_motor.config import MotorConfig
from execution_motor.errors import ConfigError
from execution_motor.halt import KillSwitch
from execution_motor.models import RejectReason
from execution_motor.motor import ExecutionMotor
from execution_motor.proto_adapter import attested_order_from_proto
from execution_motor.sor import RouterConfig, SmartOrderRouter
from execution_motor.verifiers import (
    AegisAttestationVerifier,
    RegisteredKey,
    SignatureAlgorithm,
)

from .test_aegis_fixtures import load_fixture, to_messages
from .test_motor import FakeBroker

HSM_KEY_ID = "hsm-p256-1"


def dev_key() -> RegisteredKey:
    fx = load_fixture()
    return RegisteredKey(
        fx["key_id"], SignatureAlgorithm.ED25519_DEV, bytes.fromhex(fx["public_key_hex"])
    )


def dev_verifier() -> AegisAttestationVerifier:
    return AegisAttestationVerifier([dev_key()], production=False)


def hsm_material() -> tuple[ec.EllipticCurvePrivateKey, RegisteredKey]:
    private = ec.generate_private_key(ec.SECP256R1())
    sec1 = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return private, RegisteredKey(HSM_KEY_ID, SignatureAlgorithm.ECDSA_P256_SHA256, sec1)


def hsm_sign_digest(private: ec.EllipticCurvePrivateKey, digest: bytes) -> bytes:
    """What Aegis's HSM signer does: ECDSA over the 32-byte digest, raw r || s."""
    r, s = decode_dss_signature(private.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256()))))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def hsm_text() -> bytes:
    return build_canonical_text(
        signal_id="3e7bafc0-9d94-4e31-8d87-3b4b8a7c0044",
        symbol="AAPL",
        side="BUY",
        order_type="LIMIT",
        qty_nanos=10_000_000_000,
        limit_price_nanos=150_000_000_000,
        stop_price_nanos=0,
        decided_at_ns=1_790_000_000_000_000_000,
        expires_at_ns=1_790_000_005_000_000_000,
        aegis_state_seq=1,
        limits_config_sha256="ab" * 32,
        key_id=HSM_KEY_ID,
    )


@pytest.mark.parametrize("name", ["buy", "sell", "sell_short"])
def test_real_aegis_dev_signature_verifies_over_the_digest(name: str) -> None:
    case = load_fixture()["cases"][name]
    text = case["text"].encode()
    sig = bytes.fromhex(case["attestation"]["signature_hex"])
    key_id = case["attestation"]["key_id"]
    assert dev_verifier().verify(text, sig, key_id) is True


def test_dev_signature_is_not_valid_over_the_text_itself() -> None:
    """The dev signer signs the digest, so the verifier must hash the payload first."""
    fx = load_fixture()["cases"]["buy"]
    digest = bytes.fromhex(fx["attestation"]["payload_sha256_hex"])
    sig = bytes.fromhex(fx["attestation"]["signature_hex"])
    # Passing the 32-byte digest as the "payload" hashes it again: must not verify.
    assert dev_verifier().verify(digest, sig, fx["attestation"]["key_id"]) is False


def test_dev_verifier_denies_tamper_wrong_key_and_malformed_signature() -> None:
    fx = load_fixture()["cases"]["buy"]
    text, key_id = fx["text"].encode(), fx["attestation"]["key_id"]
    sig = bytes.fromhex(fx["attestation"]["signature_hex"])
    verifier = dev_verifier()
    assert verifier.verify(text.replace(b"AAPL", b"MSFT"), sig, key_id) is False
    assert verifier.verify(text, sig, "unknown-key") is False
    assert verifier.verify(text, sig[:-1], key_id) is False
    assert verifier.verify(text, b"", key_id) is False
    assert verifier.verify(text, bytes(64), key_id) is False


def test_hsm_ecdsa_signature_verifies_over_the_text_with_sha256() -> None:
    private, key = hsm_material()
    text = hsm_text()
    sig = hsm_sign_digest(private, hashlib.sha256(text).digest())
    verifier = AegisAttestationVerifier([key], production=True)
    assert verifier.verify(text, sig, HSM_KEY_ID) is True
    assert verifier.verify(text + b"x", sig, HSM_KEY_ID) is False
    assert verifier.verify(text, sig[:-1], HSM_KEY_ID) is False
    assert verifier.verify(text, bytes(64), HSM_KEY_ID) is False
    assert verifier.accepts_dev_keys is False


def test_registry_holds_both_algorithms_and_never_mixes_them_up() -> None:
    private, hsm = hsm_material()
    verifier = AegisAttestationVerifier([dev_key(), hsm], production=False)
    fx = load_fixture()["cases"]["buy"]
    dev_sig = bytes.fromhex(fx["attestation"]["signature_hex"])
    hsm_sig = hsm_sign_digest(private, hashlib.sha256(hsm_text()).digest())
    assert verifier.verify(fx["text"].encode(), dev_sig, fx["attestation"]["key_id"]) is True
    assert verifier.verify(hsm_text(), hsm_sig, HSM_KEY_ID) is True
    # A signature under one algorithm never passes under the other key id.
    assert verifier.verify(hsm_text(), hsm_sig, fx["attestation"]["key_id"]) is False
    assert verifier.verify(fx["text"].encode(), dev_sig, HSM_KEY_ID) is False
    assert verifier.accepts_dev_keys is True


def test_dev_key_is_rejected_in_production_config() -> None:
    with pytest.raises(ConfigError, match="forbidden in production"):
        AegisAttestationVerifier([dev_key()], production=True)
    _, hsm = hsm_material()
    renamed = RegisteredKey("dev-hsm", hsm.algorithm, hsm.public_key)
    with pytest.raises(ConfigError, match="forbidden in production"):
        AegisAttestationVerifier([renamed], production=True)


def test_motor_refuses_a_dev_capable_verifier_in_production() -> None:
    cfg = MotorConfig(max_order_notional=Decimal(10_000), max_session_notional=Decimal(100_000))
    assert cfg.is_production
    with pytest.raises(ConfigError, match="dev signing keys"):
        ExecutionMotor(
            config=cfg,
            brokers={"fake": FakeBroker()},
            router=SmartOrderRouter(RouterConfig()),
            kill_switch=KillSwitch(start_halted=False),
            verifier=dev_verifier(),
        )
    dev_cfg = MotorConfig(
        max_order_notional=Decimal(10_000),
        max_session_notional=Decimal(100_000),
        environment="development",
    )
    ExecutionMotor(
        config=dev_cfg,
        brokers={"fake": FakeBroker()},
        router=SmartOrderRouter(RouterConfig()),
        kill_switch=KillSwitch(start_halted=False),
        verifier=dev_verifier(),
    )


def test_config_environment_defaults_to_production_and_validates() -> None:
    env = {"MOTOR_MAX_ORDER_NOTIONAL_USD": "1000", "MOTOR_MAX_SESSION_NOTIONAL_USD": "5000"}
    cfg = MotorConfig.from_env(env)
    assert cfg.is_production and cfg.allow_short_selling is False
    dev = MotorConfig.from_env(
        {**env, "MOTOR_ENV": "development", "MOTOR_SHORT_SELLING_ENABLED": "true"}
    )
    assert not dev.is_production and dev.allow_short_selling is True
    with pytest.raises(ConfigError):
        MotorConfig.from_env({**env, "MOTOR_ENV": "prod-ish"})
    with pytest.raises(ConfigError):
        MotorConfig.from_env({**env, "MOTOR_SHORT_SELLING_ENABLED": "yes"})


def test_real_aegis_fixture_passes_the_full_gate_with_the_real_verifier(
    pb2: dict[str, ModuleType],
) -> None:
    order, att = to_messages(pb2, load_fixture()["cases"]["buy"])
    attested = attested_order_from_proto(order, att)
    assert evaluate_attestation(dev_verifier(), attested) is None
    forged = attested.model_copy(
        update={"attestation": attested.attestation.model_copy(update={"signature": bytes(64)})}
    )
    assert evaluate_attestation(dev_verifier(), forged) is RejectReason.ATTESTATION_INVALID
