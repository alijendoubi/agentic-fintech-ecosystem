"""The motor's canonical text must equal what the REAL Aegis crate signs (fixtures generated
by tests/fixtures/gen_aegis_fixtures.sh from aegis::signing::attest::attest_order)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from execution_motor.canonical import CANONICAL_VERSION, build_canonical_text
from execution_motor.errors import OrderValidationError
from execution_motor.proto_adapter import canonical_attestation_text

FIXTURE = Path(__file__).parent / "fixtures" / "aegis_attestations.json"


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def to_messages(pb2: dict[str, ModuleType], case: dict[str, Any]) -> tuple[Any, Any]:
    o, a = case["order"], case["attestation"]
    order = pb2["order"].OrderRequest(
        order_id=o["order_id"],
        signal_id=o["signal_id"],
        symbol=o["symbol"],
        created_at_ns=o["created_at_ns"],
        side=o["side"],
        order_type=o["order_type"],
        quantity_nanos=o["quantity_nanos"],
        limit_price_nanos=o["limit_price_nanos"],
        stop_price_nanos=o["stop_price_nanos"],
        algo=o["algo"],
        status=o["status"],
        hsm_signature=bytes.fromhex(o["hsm_signature_hex"]),
        hsm_key_id=o["hsm_key_id"],
        attestation_expires_at_ns=o["attestation_expires_at_ns"],
    )
    att = pb2["aegis"].Attestation(
        canonical_version=a["canonical_version"],
        payload_sha256=bytes.fromhex(a["payload_sha256_hex"]),
        signature=bytes.fromhex(a["signature_hex"]),
        key_id=a["key_id"],
        decided_at_ns=a["decided_at_ns"],
        expires_at_ns=a["expires_at_ns"],
        aegis_state_seq=a["aegis_state_seq"],
        limits_config_sha256=a["limits_config_sha256"],
    )
    return order, att


def test_fixture_declares_the_v1_text_without_order_id() -> None:
    for case in load_fixture()["cases"].values():
        lines = case["text"].split("\n")
        assert lines[0] == CANONICAL_VERSION
        assert not any(line.startswith("order_id=") for line in lines)
        assert [line.split("=")[0] for line in lines[1:-1]] == [
            "signal_id",
            "symbol",
            "side",
            "order_type",
            "qty_nanos",
            "limit_price_nanos",
            "stop_price_nanos",
            "decided_at_ns",
            "expires_at_ns",
            "aegis_state_seq",
            "limits_config_sha256",
            "key_id",
        ]
        assert lines[-1] == ""  # every line, including the last, is newline-terminated


@pytest.mark.parametrize(
    ("name", "side_name"), [("buy", "BUY"), ("sell", "SELL"), ("sell_short", "SELL_SHORT")]
)
def test_motor_rebuilds_the_exact_aegis_text_and_digest(
    pb2: dict[str, ModuleType], name: str, side_name: str
) -> None:
    case = load_fixture()["cases"][name]
    order, att = to_messages(pb2, case)
    rebuilt = canonical_attestation_text(order, att, side_name=side_name)
    assert rebuilt.decode() == case["text"]
    assert hashlib.sha256(rebuilt).hexdigest() == case["attestation"]["payload_sha256_hex"]


def test_builder_matches_the_aegis_golden_vector() -> None:
    """Same inputs as aegis/src/signing/canonical.rs::golden_vector_text_and_digest."""
    text = build_canonical_text(
        signal_id="0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11",
        symbol="AAPL",
        side="BUY",
        order_type="LIMIT",
        qty_nanos=10_000_000_000,
        limit_price_nanos=150_000_000_000,
        stop_price_nanos=0,
        decided_at_ns=1_790_000_000_000_000_000,
        expires_at_ns=1_790_000_005_000_000_000,
        aegis_state_seq=7,
        limits_config_sha256="ab" * 32,
        key_id="dev-ed25519-1234abcd",
    )
    assert text.decode() == (
        "afe-attest-v1\nsignal_id=0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11\nsymbol=AAPL\nside=BUY\n"
        "order_type=LIMIT\nqty_nanos=10000000000\nlimit_price_nanos=150000000000\n"
        "stop_price_nanos=0\ndecided_at_ns=1790000000000000000\n"
        "expires_at_ns=1790000005000000000\naegis_state_seq=7\n"
        f"limits_config_sha256={'ab' * 32}\nkey_id=dev-ed25519-1234abcd\n"
    )


@pytest.mark.parametrize("bad", ["A\nqty_nanos=1", "A=B", "", "A\r", "A\x85"])
def test_builder_refuses_injection_like_aegis(bad: str) -> None:
    with pytest.raises(OrderValidationError):
        build_canonical_text(
            signal_id="s",
            symbol=bad,
            side="BUY",
            order_type="LIMIT",
            qty_nanos=1,
            limit_price_nanos=1,
            stop_price_nanos=0,
            decided_at_ns=1,
            expires_at_ns=2,
            aegis_state_seq=1,
            limits_config_sha256="ab",
            key_id="k",
        )
