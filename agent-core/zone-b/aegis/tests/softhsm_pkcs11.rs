//! SoftHSM2 integration test for the PKCS#11 signer (feature `pkcs11`).
//!
//! Needs a provisioned token; skipped (loudly) when the environment is not set:
//!   AEGIS_SOFTHSM_MODULE    path to libsofthsm2.so
//!   AEGIS_SOFTHSM_TOKEN     token label
//!   AEGIS_SOFTHSM_KEY       label of an EC P-256 key pair on the token
//!   AEGIS_SOFTHSM_PIN_FILE  file with the user PIN
//! See README "SoftHSM2 integration test" for the provisioning commands.
#![cfg(feature = "pkcs11")]

use std::path::PathBuf;

use aegis::config::Pkcs11Config;
use aegis::domain::{Side, ValidatedSignal};
use aegis::money::Nanos;
use aegis::pb;
use aegis::signing::attest::{attest_order, AttestParams};
use aegis::signing::hsm::{HsmBackend, HsmSigner};
use aegis::signing::pkcs11::CryptokiBackend;
use aegis::signing::verify::{verify_attestation, P256Verifier, ShortPolicy, VerifyError};
use aegis::signing::{Algorithm, SignError, Signer};

const NOW: i64 = 1_790_000_000_000_000_000;

/// One Cryptoki context at a time: each `Pkcs11` finalises the module on drop.
static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok().filter(|v| !v.is_empty())
}

fn config() -> Option<Pkcs11Config> {
    Some(Pkcs11Config {
        module: PathBuf::from(env("AEGIS_SOFTHSM_MODULE")?),
        token_label: env("AEGIS_SOFTHSM_TOKEN")?,
        key_label: env("AEGIS_SOFTHSM_KEY")?,
        pin_file: PathBuf::from(env("AEGIS_SOFTHSM_PIN_FILE")?),
    })
}

fn signal() -> ValidatedSignal {
    ValidatedSignal {
        signal_id: "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11".into(),
        symbol: "AAPL".into(),
        created_at_ns: NOW,
        valid_until_ns: NOW + 4_000_000_000,
        side: Side::Buy,
        qty: Nanos::new(10_000_000_000),
        limit_price: Some(Nanos::new(150_000_000_000)),
        omega: 0.8,
        regime: pb::RegimeLabel::TrendingBull,
        regime_confidence: 0.9,
        strategy_id: String::new(),
    }
}

fn assert_unavailable(cfg: &Pkcs11Config, needle: &str) {
    match CryptokiBackend::connect(cfg) {
        Err(SignError::Unavailable(m)) => assert!(m.contains(needle), "unexpected error: {m}"),
        Err(other) => panic!("unexpected error kind: {other:?}"),
        Ok(_) => panic!("connect must fail ({needle})"),
    }
}

#[test]
fn softhsm_signs_attestations_that_verify_and_detect_tampering() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let Some(cfg) = config() else {
        eprintln!(
            "SKIPPED softhsm_pkcs11: AEGIS_SOFTHSM_* not set (SoftHSM2 not verified in this run)"
        );
        return;
    };
    let backend = CryptokiBackend::connect(&cfg).expect("connect to SoftHSM2 token");
    let sec1 = backend.public_key_sec1().expect("read public key");
    assert_eq!(sec1.len(), 65, "uncompressed P-256 point");
    assert_eq!(backend.algorithm(), Algorithm::EcdsaP256Sha256);
    let signer = HsmSigner::new(&cfg.key_label, Box::new(backend)).expect("signer");
    assert!(signer.is_healthy());

    let sig = signal();
    let params = AttestParams {
        signal: &sig,
        price: Nanos::new(150_000_000_000),
        decided_at_ns: NOW,
        ttl_ns: 5_000_000_000,
        state_seq: 3,
        limits_sha256: &"ab".repeat(32),
    };
    let (order, att) = attest_order(&signer, &params).expect("attest");
    assert_eq!(att.signature.len(), 64);
    let verifier = P256Verifier::default()
        .with_sec1_key(signer.key_id(), &sec1)
        .expect("register key");
    assert_eq!(
        verify_attestation(&order, &att, NOW + 1, &verifier, ShortPolicy::Refuse),
        Ok(Side::Buy)
    );
    let mut tampered = order.clone();
    tampered.quantity_nanos += 1;
    assert_eq!(
        verify_attestation(&tampered, &att, NOW + 1, &verifier, ShortPolicy::Refuse),
        Err(VerifyError::DigestMismatch)
    );
}

#[test]
fn softhsm_misconfiguration_is_unavailable_not_a_panic() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let Some(good) = config() else {
        eprintln!(
            "SKIPPED softhsm_pkcs11: AEGIS_SOFTHSM_* not set (SoftHSM2 not verified in this run)"
        );
        return;
    };
    let wrong_key = Pkcs11Config {
        key_label: "no-such-key".into(),
        ..good.clone()
    };
    assert_unavailable(&wrong_key, "no signing key labelled");
    let wrong_token = Pkcs11Config {
        token_label: "no-such-token".into(),
        ..good.clone()
    };
    assert_unavailable(&wrong_token, "no token with label");
    let wrong_pin_file = Pkcs11Config {
        pin_file: PathBuf::from("/nonexistent/pin"),
        ..good.clone()
    };
    assert_unavailable(&wrong_pin_file, "read pin file");
    let missing_module = Pkcs11Config {
        module: PathBuf::from("/nonexistent/lib.so"),
        ..good
    };
    assert_unavailable(&missing_module, "load module");
}
