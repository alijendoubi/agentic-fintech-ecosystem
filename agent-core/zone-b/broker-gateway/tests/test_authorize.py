"""SubmitAuthoriser: every rule that stands between a submit and the broker credentials."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from execution_motor.canonical import build_canonical_text

from broker_gateway.authorize import (
    Refusal,
    Refused,
    ReplayStore,
    broker_body,
    nanos_to_decimal_text,
    parse_canonical_text,
)

from .helpers import (
    AEGIS_FIXTURE,
    NOW,
    SECOND,
    Clock,
    DevSigner,
    HsmSigner,
    intent_for,
    make_authoriser,
    make_proof,
)


def refusal_of(fn: Any) -> Refusal:
    with pytest.raises(Refused) as info:
        fn()
    return info.value.reason


# ------------------------------------------------------------------------- accepted


@pytest.mark.parametrize("signer_cls", [DevSigner, HsmSigner])
def test_valid_attestation_is_authorised_and_body_comes_from_attested_fields(
    signer_cls: type[DevSigner] | type[HsmSigner],
) -> None:
    signer = signer_cls()
    proof = make_proof(signer)
    result = make_authoriser(signer).authorise(
        proof.text, proof.signature, proof.key_id, intent_for(proof.fields)
    )
    assert result.body == {
        "symbol": "AAPL",
        "qty": "10",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "client_order_id": proof.fields["signal_id"],
        "limit_price": "150.5",
    }


@pytest.mark.parametrize("case", ["buy", "sell", "sell_short"])
def test_real_aegis_fixture_text_is_accepted(case: str) -> None:
    """Cross-language parity: text and signature produced by the Rust Aegis crate."""
    from execution_motor.verifiers import AegisAttestationVerifier, RegisteredKey
    from execution_motor.verifiers import SignatureAlgorithm as Algo

    from broker_gateway.authorize import SubmitAuthoriser

    fx = json.loads(AEGIS_FIXTURE.read_text(encoding="utf-8"))
    data = fx["cases"][case]
    text = data["text"].encode()
    fields = parse_canonical_text(text).__dict__
    key = RegisteredKey(fx["key_id"], Algo.ED25519_DEV, bytes.fromhex(fx["public_key_hex"]))
    authoriser = SubmitAuthoriser(
        AegisAttestationVerifier([key], production=False),
        max_ttl_ns=5 * SECOND,
        max_clock_skew_ns=SECOND,
        started_at_ns=fx["now_ns"] - 60 * SECOND,
        clock_ns=lambda: fx["now_ns"] + SECOND,
    )
    result = authoriser.authorise(
        text,
        bytes.fromhex(data["attestation"]["signature_hex"]),
        fx["key_id"],
        intent_for(fields),
    )
    assert result.body["side"] == ("buy" if case == "buy" else "sell")


def test_market_and_stop_orders_carry_only_their_price_fields() -> None:
    signer = DevSigner()
    for order_type, limit, stop, expected in (
        ("MARKET", 0, 0, set()),
        ("STOP", 0, 149 * SECOND, {"stop_price"}),
        ("STOP_LIMIT", 148 * SECOND, 149 * SECOND, {"limit_price", "stop_price"}),
    ):
        proof = make_proof(
            signer,
            order_type=order_type,
            limit_price_nanos=limit,
            stop_price_nanos=stop,
            signal_id=f"sig-{order_type}",
        )
        body = (
            make_authoriser(signer)
            .authorise(proof.text, proof.signature, proof.key_id, intent_for(proof.fields))
            .body
        )
        assert set(body) & {"limit_price", "stop_price"} == expected
        assert body["type"] == order_type.lower()


def test_nanos_formatting_is_exact() -> None:
    assert nanos_to_decimal_text(10 * SECOND) == "10"
    assert nanos_to_decimal_text(1) == "0.000000001"
    assert nanos_to_decimal_text(150_500_000_000) == "150.5"
    assert nanos_to_decimal_text(123_456_789_012_345) == "123456.789012345"


# ------------------------------------------------------------------ field mismatches

_MISMATCHES: list[tuple[str, Any]] = [
    ("client_order_id", "attacker-chosen-id"),
    ("symbol", "MSFT"),
    ("side", "sell"),
    ("order_type", "market"),
    ("qty_nanos", 10 * SECOND + 1),
    ("qty_nanos", 100 * SECOND),
    ("limit_price_nanos", 151 * SECOND),
    ("limit_price_nanos", 0),
    ("stop_price_nanos", 1),
]


@pytest.mark.parametrize(("field", "value"), _MISMATCHES)
def test_every_field_mismatch_is_refused(field: str, value: Any) -> None:
    signer = DevSigner()
    proof = make_proof(signer)
    intent = replace(intent_for(proof.fields), **{field: value})
    authoriser = make_authoriser(signer)
    with pytest.raises(Refused) as info:
        authoriser.authorise(proof.text, proof.signature, proof.key_id, intent)
    assert info.value.reason is Refusal.ORDER_MISMATCH
    assert field in info.value.detail
    # A refused mismatch does not burn the signal: the genuine order still goes through.
    authoriser.authorise(proof.text, proof.signature, proof.key_id, intent_for(proof.fields))


def test_attested_sell_short_is_sent_as_sell_and_buy_intent_is_refused() -> None:
    signer = DevSigner()
    proof = make_proof(signer, side="SELL_SHORT")
    authoriser = make_authoriser(signer)
    bad = replace(intent_for(proof.fields), side="buy")
    assert refusal_of(
        lambda: authoriser.authorise(proof.text, proof.signature, proof.key_id, bad)
    ) is (Refusal.ORDER_MISMATCH)
    ok = authoriser.authorise(proof.text, proof.signature, proof.key_id, intent_for(proof.fields))
    assert ok.body["side"] == "sell"


@pytest.mark.parametrize("tif", ["gtc", "ioc", "opg", "", "DAY"])
def test_only_day_time_in_force_is_accepted(tif: str) -> None:
    signer = DevSigner()
    proof = make_proof(signer)
    intent = replace(intent_for(proof.fields), time_in_force=tif)
    assert refusal_of(
        lambda: make_authoriser(signer).authorise(proof.text, proof.signature, proof.key_id, intent)
    ) is (Refusal.TIME_IN_FORCE_NOT_ALLOWED)


# ------------------------------------------------------------------------ signature


def test_bad_signature_is_refused() -> None:
    signer = DevSigner()
    proof = make_proof(signer)
    sig = bytearray(proof.signature)
    sig[0] ^= 1
    assert refusal_of(
        lambda: make_authoriser(signer).authorise(
            proof.text, bytes(sig), proof.key_id, intent_for(proof.fields)
        )
    ) is (Refusal.SIGNATURE_INVALID)


def test_tampered_text_with_the_original_signature_is_refused() -> None:
    signer = DevSigner()
    proof = make_proof(signer)
    forged_fields = dict(proof.fields, qty_nanos=100 * SECOND)
    forged = build_canonical_text(**forged_fields)
    assert refusal_of(
        lambda: make_authoriser(signer).authorise(
            forged, proof.signature, proof.key_id, intent_for(forged_fields)
        )
    ) is (Refusal.SIGNATURE_INVALID)


def test_attacker_key_under_the_registered_key_id_is_refused() -> None:
    real, attacker = DevSigner(), DevSigner()  # same key_id, different key
    proof = make_proof(attacker)
    assert refusal_of(
        lambda: make_authoriser(real).authorise(
            proof.text, proof.signature, proof.key_id, intent_for(proof.fields)
        )
    ) is (Refusal.SIGNATURE_INVALID)


def test_unknown_key_id_is_refused() -> None:
    registered, other = DevSigner(), DevSigner("dev-ed25519-unregistered")
    proof = make_proof(other)
    assert refusal_of(
        lambda: make_authoriser(registered).authorise(
            proof.text, proof.signature, proof.key_id, intent_for(proof.fields)
        )
    ) is (Refusal.SIGNATURE_INVALID)


def test_proof_key_id_must_equal_the_signed_key_id_line() -> None:
    a, b = DevSigner("dev-ed25519-a"), DevSigner("dev-ed25519-b")
    proof = make_proof(a)
    assert refusal_of(
        lambda: make_authoriser(a, b).authorise(
            proof.text, proof.signature, b.key_id, intent_for(proof.fields)
        )
    ) is (Refusal.KEY_ID_MISMATCH)


@pytest.mark.parametrize("verdict", [None, 1, "yes", RuntimeError("boom")])
def test_verifier_errors_and_truthy_junk_are_denials(verdict: Any) -> None:
    from broker_gateway.authorize import SubmitAuthoriser

    class Junk:
        def verify(self, payload: bytes, signature: bytes, key_id: str) -> bool:
            if isinstance(verdict, Exception):
                raise verdict
            return verdict  # type: ignore[no-any-return]

    signer = DevSigner()
    proof = make_proof(signer)
    authoriser = SubmitAuthoriser(
        Junk(), max_ttl_ns=5 * SECOND, max_clock_skew_ns=SECOND, started_at_ns=0, clock_ns=Clock()
    )
    assert refusal_of(
        lambda: authoriser.authorise(
            proof.text, proof.signature, proof.key_id, intent_for(proof.fields)
        )
    ) is (Refusal.SIGNATURE_INVALID)


@pytest.mark.parametrize("missing", ["text", "signature", "key_id"])
def test_missing_attestation_parts_are_refused(missing: str) -> None:
    signer = DevSigner()
    proof = make_proof(signer)
    args = {"text": proof.text, "signature": proof.signature, "key_id": proof.key_id}
    args[missing] = b"" if missing != "key_id" else ""
    assert refusal_of(
        lambda: make_authoriser(signer).authorise(
            args["text"],  # type: ignore[arg-type]
            args["signature"],  # type: ignore[arg-type]
            args["key_id"],  # type: ignore[arg-type]
            intent_for(proof.fields),
        )
    ) is (Refusal.MALFORMED_REQUEST)


# ------------------------------------------------------------------ text strictness


def test_v1_text_is_refused_even_when_validly_signed() -> None:
    signer = DevSigner()
    fields = make_proof(signer).fields
    v2 = build_canonical_text(**fields)
    v1 = v2.replace(b"afe-attest-v2\n", b"afe-attest-v1\n").replace(
        f"strategy_id={fields['strategy_id']}\n".encode(), b""
    )
    assert refusal_of(
        lambda: make_authoriser(signer).authorise(
            v1, signer.sign(v1), signer.key_id, intent_for(fields)
        )
    ) is (Refusal.UNSUPPORTED_VERSION)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-1],  # not newline-terminated
        lambda t: t + b"extra=1\n",  # extra line
        lambda t: t.replace(b"qty_nanos=10000000000", b"qty_nanos=010000000000"),
        lambda t: t.replace(b"qty_nanos=10000000000", b"qty_nanos=+10000000000"),
        lambda t: t.replace(b"qty_nanos=10000000000", b"qty_nanos=-1"),
        lambda t: t.replace(b"symbol=AAPL\nside=BUY", b"side=BUY\nsymbol=AAPL"),  # order
        lambda t: t.replace(b"side=BUY", b"side=HOLD"),
        lambda t: t.replace(b"order_type=LIMIT", b"order_type=limit"),
        lambda t: t.replace(b"\n", b"\r\n"),
        lambda t: t.replace(b"symbol=AAPL", b"symbol=AA\x01PL"),
        lambda t: t.replace(b"symbol=AAPL", b"symbol=\xff"),
        lambda t: t.replace(b"symbol=AAPL", b"symbol="),
    ],
)
def test_non_canonical_text_is_refused_before_the_signature_is_trusted(mutate: Any) -> None:
    signer = DevSigner()
    proof = make_proof(signer)
    text = mutate(proof.text)
    with pytest.raises(Refused) as info:
        # Signed by the real key: only strict parsing can stop it.
        make_authoriser(signer).authorise(
            text, signer.sign(text), signer.key_id, intent_for(proof.fields)
        )
    assert info.value.reason in (Refusal.NON_CANONICAL_TEXT, Refusal.UNSUPPORTED_VERSION)


# --------------------------------------------------------------------------- timing


def _authorise_at(now: int, **proof_overrides: Any) -> Refusal | None:
    signer = DevSigner()
    proof = make_proof(signer, **proof_overrides)
    try:
        make_authoriser(signer, clock=Clock(now)).authorise(
            proof.text, proof.signature, proof.key_id, intent_for(proof.fields)
        )
    except Refused as exc:
        return exc.reason
    return None


def test_expired_attestation_is_refused() -> None:
    assert _authorise_at(NOW + 5 * SECOND + 1) is Refusal.EXPIRED


def test_attestation_at_the_exact_expiry_instant_is_refused() -> None:
    assert _authorise_at(NOW + 5 * SECOND) is Refusal.EXPIRED


def test_attestation_one_nanosecond_before_expiry_is_accepted() -> None:
    assert _authorise_at(NOW + 5 * SECOND - 1) is None


def test_attestation_from_the_future_beyond_skew_is_refused() -> None:
    assert _authorise_at(NOW - SECOND - 1) is Refusal.FROM_FUTURE
    assert _authorise_at(NOW - SECOND) is None  # within the 1 s skew allowance


def test_lifetime_longer_than_the_maximum_is_refused() -> None:
    assert _authorise_at(NOW + SECOND, expires_at_ns=NOW + 5 * SECOND + 1) is (
        Refusal.LIFETIME_TOO_LONG
    )


def test_attestation_decided_before_the_gateway_started_is_refused() -> None:
    """A restart empties the in-memory replay store; anything a previous process might have
    forwarded must therefore be refused."""
    signer = DevSigner()
    proof = make_proof(signer)
    started = NOW - SECOND // 2  # started half a second before the decision: within skew
    authoriser = make_authoriser(signer, started_at_ns=started)
    assert refusal_of(
        lambda: authoriser.authorise(
            proof.text, proof.signature, proof.key_id, intent_for(proof.fields)
        )
    ) is (Refusal.PREDATES_GATEWAY_START)


# --------------------------------------------------------------------------- replay


def test_replayed_attestation_is_refused() -> None:
    signer = DevSigner()
    proof = make_proof(signer)
    authoriser = make_authoriser(signer)
    authoriser.authorise(proof.text, proof.signature, proof.key_id, intent_for(proof.fields))
    assert refusal_of(
        lambda: authoriser.authorise(
            proof.text, proof.signature, proof.key_id, intent_for(proof.fields)
        )
    ) is (Refusal.REPLAYED)


def test_same_signal_id_under_a_different_attestation_is_still_a_replay() -> None:
    signer = DevSigner()
    first = make_proof(signer)
    second = make_proof(signer, aegis_state_seq=8)  # re-signed, same signal_id
    authoriser = make_authoriser(signer)
    authoriser.authorise(first.text, first.signature, first.key_id, intent_for(first.fields))
    assert refusal_of(
        lambda: authoriser.authorise(
            second.text, second.signature, second.key_id, intent_for(second.fields)
        )
    ) is (Refusal.REPLAYED)


def test_replay_store_prunes_only_entries_that_can_no_longer_pass_expiry() -> None:
    store = ReplayStore(retention_after_expiry_ns=SECOND)
    assert store.claim("a", expires_at_ns=NOW, now_ns=NOW - SECOND)
    assert not store.claim("a", expires_at_ns=NOW, now_ns=NOW + SECOND)  # still retained
    assert store.claim("b", expires_at_ns=NOW + 10 * SECOND, now_ns=NOW + SECOND + 1)
    assert len(store) == 1  # "a" pruned: expired more than the retention ago


def test_broker_body_uses_signal_id_as_client_order_id() -> None:
    signer = DevSigner()
    order = parse_canonical_text(make_proof(signer).text)
    assert broker_body(order, "day")["client_order_id"] == order.signal_id
