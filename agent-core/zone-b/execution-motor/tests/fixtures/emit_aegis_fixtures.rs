//! Generates tests/fixtures/aegis_attestations.json from the REAL Aegis signing code.
//! Not compiled in this repo: gen_aegis_fixtures.sh copies it into a scratch copy of the
//! aegis crate (examples/) inside the afe-rust-dev container and runs it.
use aegis::config::Environment;
use aegis::domain::{Side, ValidatedSignal};
use aegis::hex;
use aegis::money::Nanos;
use aegis::pb;
use aegis::signing::attest::{attest_order, AttestParams};
use aegis::signing::canonical::AttestationFields;
use aegis::signing::dev::DevEd25519Signer;
use aegis::signing::Signer;
use serde_json::json;

const NOW: i64 = 1_790_000_000_000_000_000;
const TTL_NS: i64 = 5_000_000_000;
const LIMITS_SHA: &str = "abababababababababababababababababababababababababababababababab";

fn signal(side: Side, id: &str) -> ValidatedSignal {
    ValidatedSignal {
        signal_id: id.into(),
        symbol: "AAPL".into(),
        created_at_ns: NOW,
        valid_until_ns: NOW + 4_000_000_000,
        side,
        qty: Nanos::new(10_000_000_000),
        limit_price: Some(Nanos::new(150_000_000_000)),
        omega: 0.8,
        regime: pb::RegimeLabel::TrendingBull,
        regime_confidence: 0.9,
        strategy_id: String::new(),
    }
}

fn main() {
    let signer = DevEd25519Signer::from_seed(Environment::Test, [7u8; 32]).unwrap();
    let pubkey = hex::encode(signer.verifying_key().as_bytes());
    let mut cases = serde_json::Map::new();
    let specs = [
        ("buy", Side::Buy, "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11"),
        ("sell", Side::Sell, "1c5f8dae-7b72-4c1f-8b65-1f2f6e4fa022"),
        ("sell_short", Side::SellShort, "2d6a9ebf-8c83-4d20-9c76-2a3a7f5ab033"),
    ];
    for (name, side, id) in specs {
        let sig = signal(side, id);
        let (order, att) = attest_order(
            &signer,
            &AttestParams {
                signal: &sig,
                price: Nanos::new(150_000_000_000),
                decided_at_ns: NOW,
                ttl_ns: TTL_NS,
                state_seq: 7,
                limits_sha256: LIMITS_SHA,
            },
        )
        .unwrap();
        let fields = AttestationFields {
            signal_id: sig.signal_id.clone(),
            symbol: sig.symbol.clone(),
            side,
            order_type: "LIMIT".into(),
            qty_nanos: order.quantity_nanos,
            limit_price_nanos: order.limit_price_nanos,
            stop_price_nanos: order.stop_price_nanos,
            decided_at_ns: att.decided_at_ns,
            expires_at_ns: att.expires_at_ns,
            aegis_state_seq: att.aegis_state_seq,
            limits_config_sha256: att.limits_config_sha256.clone(),
            key_id: att.key_id.clone(),
        };
        cases.insert(
            name.into(),
            json!({
                "text": fields.canonical_text().unwrap(),
                "order": {
                    "order_id": order.order_id, "signal_id": order.signal_id,
                    "symbol": order.symbol, "created_at_ns": order.created_at_ns,
                    "side": order.side, "order_type": order.order_type,
                    "quantity_nanos": order.quantity_nanos,
                    "limit_price_nanos": order.limit_price_nanos,
                    "stop_price_nanos": order.stop_price_nanos,
                    "algo": order.algo, "status": order.status,
                    "hsm_signature_hex": hex::encode(&order.hsm_signature),
                    "hsm_key_id": order.hsm_key_id,
                    "attestation_expires_at_ns": order.attestation_expires_at_ns,
                },
                "attestation": {
                    "canonical_version": att.canonical_version,
                    "payload_sha256_hex": hex::encode(&att.payload_sha256),
                    "signature_hex": hex::encode(&att.signature),
                    "key_id": att.key_id,
                    "decided_at_ns": att.decided_at_ns,
                    "expires_at_ns": att.expires_at_ns,
                    "aegis_state_seq": att.aegis_state_seq,
                    "limits_config_sha256": att.limits_config_sha256,
                },
            }),
        );
    }
    let out = json!({
        "generator": "aegis attest_order + DevEd25519Signer::from_seed(Test, [7;32])",
        "algorithm": "ED25519_OVER_PAYLOAD_SHA256",
        "key_id": signer.key_id(),
        "public_key_hex": pubkey,
        "now_ns": NOW,
        "cases": cases,
    });
    println!("{}", serde_json::to_string_pretty(&out).unwrap());
}
