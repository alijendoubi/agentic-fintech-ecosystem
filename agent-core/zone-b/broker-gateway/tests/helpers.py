"""Test doubles: attestation signers (same schemes as Aegis), a fake Alpaca, a fake gRPC context.

No network anywhere: the fake Alpaca is an ``httpx.MockTransport`` handler, so every request
the gateway makes is recorded in memory.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from execution_motor.canonical import build_canonical_text
from execution_motor.verifiers import AegisAttestationVerifier, RegisteredKey, SignatureAlgorithm

from broker_gateway.alpaca import AlpacaCredentials, AlpacaForwarder
from broker_gateway.authorize import Intent, SubmitAuthoriser

API_KEY = "PKGATEWAYKEY0001"
SECRET = "GATEWAYSECRETVALUE0002"
CREDS = AlpacaCredentials(api_key=API_KEY, secret_key=SECRET)

NOW = 1_790_000_000_000_000_000
SECOND = 1_000_000_000
SIGNAL = "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11"
MOTOR_DIR = Path(__file__).resolve().parents[2] / "execution-motor"
AEGIS_FIXTURE = MOTOR_DIR / "tests" / "fixtures" / "aegis_attestations.json"


class DevSigner:
    """Aegis dev signer scheme: Ed25519 over SHA-256(text)."""

    def __init__(self, key_id: str = "dev-ed25519-test0001") -> None:
        self.key_id = key_id
        self._key = Ed25519PrivateKey.generate()
        self.registered = RegisteredKey(
            key_id,
            SignatureAlgorithm.ED25519_DEV,
            self._key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            ),
        )

    def sign(self, text: bytes) -> bytes:
        return self._key.sign(hashlib.sha256(text).digest())


class HsmSigner:
    """Aegis PKCS#11 scheme: ECDSA P-256 over SHA-256(text), raw r||s."""

    def __init__(self, key_id: str = "hsm-p256-test0001") -> None:
        self.key_id = key_id
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.registered = RegisteredKey(
            key_id,
            SignatureAlgorithm.ECDSA_P256_SHA256,
            self._key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            ),
        )

    def sign(self, text: bytes) -> bytes:
        r, s = decode_dss_signature(self._key.sign(text, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def attested_fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "signal_id": SIGNAL,
        "strategy_id": "AFE-STRATEGY-001",
        "symbol": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "qty_nanos": 10 * SECOND,
        "limit_price_nanos": 150_500_000_000,
        "stop_price_nanos": 0,
        "decided_at_ns": NOW,
        "expires_at_ns": NOW + 5 * SECOND,
        "aegis_state_seq": 7,
        "limits_config_sha256": "ab" * 32,
        "key_id": "dev-ed25519-test0001",
    }
    fields.update(overrides)
    return fields


def intent_for(fields: dict[str, Any], **overrides: Any) -> Intent:
    values: dict[str, Any] = {
        "client_order_id": fields["signal_id"],
        "symbol": fields["symbol"],
        "side": {"BUY": "buy", "SELL": "sell", "SELL_SHORT": "sell"}[fields["side"]],
        "order_type": fields["order_type"].lower(),
        "qty_nanos": fields["qty_nanos"],
        "limit_price_nanos": fields["limit_price_nanos"],
        "stop_price_nanos": fields["stop_price_nanos"],
        "time_in_force": "day",
    }
    values.update(overrides)
    return Intent(**values)


@dataclass
class Proof:
    text: bytes
    signature: bytes
    key_id: str
    fields: dict[str, Any]


def make_proof(signer: DevSigner | HsmSigner, **overrides: Any) -> Proof:
    fields = attested_fields(key_id=signer.key_id, **overrides)
    text = build_canonical_text(**fields)
    return Proof(text=text, signature=signer.sign(text), key_id=signer.key_id, fields=fields)


def order_json(client_order_id: str = SIGNAL, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "904837e3-3b76-47ec-b432-046db621571b",
        "client_order_id": client_order_id,
        "submitted_at": "2026-09-20T10:00:00.223456789Z",
        "filled_at": None,
        "symbol": "AAPL",
        "qty": "10",
        "filled_qty": "0",
        "filled_avg_price": None,
        "status": "new",
    }
    body.update(overrides)
    return body


@dataclass
class FakeAlpaca:
    """Records every request; answers like the (ASSUMED) Alpaca v2 API."""

    requests: list[httpx.Request] = field(default_factory=list)
    fail_with: Exception | None = None
    status: int = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        path = request.url.path
        if request.method == "POST" and path == "/v2/orders":
            body = json.loads(request.content)
            return httpx.Response(self.status, json=order_json(body["client_order_id"]))
        if request.method == "GET" and path == "/v2/orders:by_client_order_id":
            return httpx.Response(200, json=order_json(request.url.params["client_order_id"]))
        if request.method == "GET" and path == "/v2/orders":
            return httpx.Response(200, json=[order_json()])
        if request.method == "DELETE" and path.startswith("/v2/orders/"):
            return httpx.Response(204)
        if request.method == "GET" and path == "/v2/positions":
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/v2/account":
            return httpx.Response(200, json={"status": "ACTIVE", "equity": "100000"})
        return httpx.Response(404, json={"message": "not found"})

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]


class AbortError(Exception):
    def __init__(self, code: Any, details: str) -> None:
        super().__init__(details)
        self.code = code


class FakeContext:
    """The two parts of ``grpc.ServicerContext`` the servicer uses."""

    def __init__(self, cn: str | None = "execution-motor") -> None:
        self._cn = cn

    def auth_context(self) -> dict[str, list[bytes]]:
        return {} if self._cn is None else {"x509_common_name": [self._cn.encode()]}

    def abort(self, code: Any, details: str) -> None:
        raise AbortError(code, details)


@dataclass
class Clock:
    now: int = NOW + SECOND // 10

    def __call__(self) -> int:
        return self.now


def make_authoriser(
    *signers: DevSigner | HsmSigner,
    clock: Clock | None = None,
    started_at_ns: int = NOW - 60 * SECOND,
    max_ttl_ns: int = 5 * SECOND,
    skew_ns: int = SECOND,
) -> SubmitAuthoriser:
    verifier = AegisAttestationVerifier([s.registered for s in signers], production=False)
    return SubmitAuthoriser(
        verifier,
        max_ttl_ns=max_ttl_ns,
        max_clock_skew_ns=skew_ns,
        started_at_ns=started_at_ns,
        clock_ns=clock or Clock(),
    )


def make_forwarder(fake: FakeAlpaca) -> AlpacaForwarder:
    return AlpacaForwarder(CREDS, transport=httpx.MockTransport(fake))
