//! Attestation verification helper. Used by Aegis's own tests and documented
//! for the broker gateway / execution-motor (README "Attestation contract").
//!
//! A verifier MUST, in this order:
//!  1. check `canonical_version == "afe-attest-v1"`,
//!  2. check the order mirrors the attestation (`hsm_key_id`, `hsm_signature`,
//!     `attestation_expires_at_ns`),
//!  3. reject if `now > expires_at_ns` (or `now < decided_at_ns` beyond skew),
//!  4. rebuild the canonical text from the ORDER fields plus the attestation
//!     fields, hash it and compare with `payload_sha256`,
//!  5. verify the signature over the digest with the key registered for `key_id`,
//!  6. enforce single use (a `signal_id` / `order_id` seen before is refused).
//!     Step 6 needs state and belongs to the gateway; it is not done here.
//!
//! Side mapping: `ORDER_BUY` -> `BUY`; `ORDER_SELL` -> `SELL` or `SELL_SHORT`
//! (the verifier tries both; the one that matches the signed digest is
//! returned). A gateway that does not allow short sales MUST refuse
//! `SELL_SHORT`, which is what [`ShortPolicy::Refuse`] does.

use std::collections::HashMap;

use ed25519_dalek::{Signature, Verifier as _, VerifyingKey};
use thiserror::Error;

use super::canonical::{AttestationFields, CANONICAL_VERSION};
use crate::domain::Side;
use crate::pb;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum VerifyError {
    #[error("unsupported canonical version")]
    UnsupportedVersion,
    #[error("order does not mirror the attestation ({0})")]
    OrderAttestationMismatch(&'static str),
    #[error("order_id must equal signal_id (the client order id is bound through the signal id)")]
    OrderIdNotBound,
    #[error("attestation expired")]
    Expired,
    #[error("attestation decided in the future")]
    FromFuture,
    #[error("unknown or unsupported order side/type")]
    UnsupportedOrder,
    #[error("order fields do not match the signed digest")]
    DigestMismatch,
    #[error("unknown signing key")]
    UnknownKey,
    #[error("bad signature")]
    BadSignature,
    #[error("short sale not permitted")]
    ShortNotPermitted,
}

/// Signature check for a digest under a registered key.
pub trait Verifier {
    fn verify(&self, key_id: &str, digest: &[u8], signature: &[u8]) -> Result<(), VerifyError>;
}

/// Registry of Ed25519 public keys by key id (dev signer).
#[derive(Debug, Default)]
pub struct Ed25519Verifier {
    keys: HashMap<String, VerifyingKey>,
}

impl Ed25519Verifier {
    pub fn with_key(mut self, key_id: &str, key: VerifyingKey) -> Self {
        self.keys.insert(key_id.to_owned(), key);
        self
    }
}

impl Verifier for Ed25519Verifier {
    fn verify(&self, key_id: &str, digest: &[u8], signature: &[u8]) -> Result<(), VerifyError> {
        let key = self.keys.get(key_id).ok_or(VerifyError::UnknownKey)?;
        let sig = Signature::from_slice(signature).map_err(|_| VerifyError::BadSignature)?;
        key.verify(digest, &sig)
            .map_err(|_| VerifyError::BadSignature)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShortPolicy {
    Refuse,
    Allow,
}

/// Clock-skew tolerance when checking `decided_at_ns`.
pub const DECIDED_AT_SKEW_NS: i64 = 250_000_000;

fn order_type_name(raw: i32) -> Option<&'static str> {
    match pb::OrderType::try_from(raw) {
        Ok(pb::OrderType::Limit) => Some("LIMIT"),
        Ok(pb::OrderType::Market) => Some("MARKET"),
        Ok(pb::OrderType::Stop) => Some("STOP"),
        Ok(pb::OrderType::StopLimit) => Some("STOP_LIMIT"),
        _ => None,
    }
}

fn candidate_sides(raw: i32) -> &'static [Side] {
    match pb::OrderSide::try_from(raw) {
        Ok(pb::OrderSide::OrderBuy) => &[Side::Buy],
        Ok(pb::OrderSide::OrderSell) => &[Side::Sell, Side::SellShort],
        _ => &[],
    }
}

fn check_mirror(order: &pb::OrderRequest, a: &pb::Attestation) -> Result<(), VerifyError> {
    use VerifyError::OrderAttestationMismatch as M;
    if order.hsm_key_id != a.key_id {
        return Err(M("hsm_key_id"));
    }
    if order.hsm_signature != a.signature {
        return Err(M("hsm_signature"));
    }
    if order.attestation_expires_at_ns != a.expires_at_ns {
        return Err(M("attestation_expires_at_ns"));
    }
    Ok(())
}

fn fields_for(
    order: &pb::OrderRequest,
    a: &pb::Attestation,
    side: Side,
    order_type: &str,
) -> AttestationFields {
    AttestationFields {
        signal_id: order.signal_id.clone(),
        symbol: order.symbol.clone(),
        side,
        order_type: order_type.to_owned(),
        qty_nanos: order.quantity_nanos,
        limit_price_nanos: order.limit_price_nanos,
        stop_price_nanos: order.stop_price_nanos,
        decided_at_ns: a.decided_at_ns,
        expires_at_ns: a.expires_at_ns,
        aegis_state_seq: a.aegis_state_seq,
        limits_config_sha256: a.limits_config_sha256.clone(),
        key_id: a.key_id.clone(),
    }
}

/// Verify that `order` is exactly what Aegis attested and that the attestation
/// is unexpired. Returns the attested side (`SELL` vs `SELL_SHORT`).
pub fn verify_attestation(
    order: &pb::OrderRequest,
    attestation: &pb::Attestation,
    now_ns: i64,
    verifier: &dyn Verifier,
    short_policy: ShortPolicy,
) -> Result<Side, VerifyError> {
    if attestation.canonical_version != CANONICAL_VERSION {
        return Err(VerifyError::UnsupportedVersion);
    }
    check_mirror(order, attestation)?;
    if order.order_id != order.signal_id {
        return Err(VerifyError::OrderIdNotBound);
    }
    if now_ns > attestation.expires_at_ns {
        return Err(VerifyError::Expired);
    }
    if attestation.decided_at_ns > now_ns.saturating_add(DECIDED_AT_SKEW_NS) {
        return Err(VerifyError::FromFuture);
    }
    let order_type = order_type_name(order.order_type).ok_or(VerifyError::UnsupportedOrder)?;
    let sides = candidate_sides(order.side);
    if sides.is_empty() {
        return Err(VerifyError::UnsupportedOrder);
    }
    for side in sides {
        let digest = fields_for(order, attestation, *side, order_type)
            .digest()
            .map_err(|_| VerifyError::UnsupportedOrder)?;
        if digest.as_slice() != attestation.payload_sha256.as_slice() {
            continue;
        }
        verifier.verify(&attestation.key_id, &digest, &attestation.signature)?;
        if *side == Side::SellShort && short_policy == ShortPolicy::Refuse {
            return Err(VerifyError::ShortNotPermitted);
        }
        return Ok(*side);
    }
    Err(VerifyError::DigestMismatch)
}

/// P-256 verifier (feature `pkcs11`): checks the HSM's raw `r || s` signature
/// over the 32-byte digest.
#[cfg(feature = "pkcs11")]
#[derive(Debug, Default)]
pub struct P256Verifier {
    keys: HashMap<String, p256::ecdsa::VerifyingKey>,
}

#[cfg(feature = "pkcs11")]
impl P256Verifier {
    /// `sec1` is the uncompressed point (`0x04 || X || Y`, 65 bytes).
    pub fn with_sec1_key(mut self, key_id: &str, sec1: &[u8]) -> Result<Self, VerifyError> {
        let key = p256::ecdsa::VerifyingKey::from_sec1_bytes(sec1)
            .map_err(|_| VerifyError::UnknownKey)?;
        self.keys.insert(key_id.to_owned(), key);
        Ok(self)
    }
}

#[cfg(feature = "pkcs11")]
impl Verifier for P256Verifier {
    fn verify(&self, key_id: &str, digest: &[u8], signature: &[u8]) -> Result<(), VerifyError> {
        use p256::ecdsa::signature::hazmat::PrehashVerifier as _;
        let key = self.keys.get(key_id).ok_or(VerifyError::UnknownKey)?;
        let sig =
            p256::ecdsa::Signature::from_slice(signature).map_err(|_| VerifyError::BadSignature)?;
        key.verify_prehash(digest, &sig)
            .map_err(|_| VerifyError::BadSignature)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Environment;
    use crate::domain::ValidatedSignal;
    use crate::money::Nanos;
    use crate::signing::attest::{attest_order, AttestParams};
    use crate::signing::dev::DevEd25519Signer;
    use crate::signing::Signer;

    const NOW: i64 = 1_790_000_000_000_000_000;
    const TTL_NS: i64 = 5_000_000_000;

    fn signal(side: Side) -> ValidatedSignal {
        ValidatedSignal {
            signal_id: "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11".into(),
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

    struct Rig {
        order: pb::OrderRequest,
        att: pb::Attestation,
        verifier: Ed25519Verifier,
    }

    fn rig(side: Side) -> Rig {
        let signer = DevEd25519Signer::from_seed(Environment::Test, [5; 32]).unwrap();
        let sig = signal(side);
        let params = AttestParams {
            signal: &sig,
            price: Nanos::new(150_000_000_000),
            decided_at_ns: NOW,
            ttl_ns: TTL_NS,
            state_seq: 9,
            limits_sha256: &"ab".repeat(32),
        };
        let (order, att) = attest_order(&signer, &params).unwrap();
        let verifier = Ed25519Verifier::default().with_key(signer.key_id(), signer.verifying_key());
        Rig {
            order,
            att,
            verifier,
        }
    }

    fn check(r: &Rig, now: i64, p: ShortPolicy) -> Result<Side, VerifyError> {
        verify_attestation(&r.order, &r.att, now, &r.verifier, p)
    }

    #[test]
    fn a_fresh_attestation_verifies_and_mirrors_the_order() {
        let r = rig(Side::Buy);
        assert_eq!(check(&r, NOW + 1, ShortPolicy::Refuse), Ok(Side::Buy));
        assert_eq!(r.att.expires_at_ns, NOW + TTL_NS);
        assert_eq!(r.order.attestation_expires_at_ns, r.att.expires_at_ns);
        assert_eq!(r.order.hsm_signature, r.att.signature);
        assert_eq!(r.order.side, pb::OrderSide::OrderBuy as i32);
        assert_eq!(r.order.order_type, pb::OrderType::Limit as i32);
        assert_eq!(r.att.canonical_version, "afe-attest-v1");
        assert_eq!(r.att.payload_sha256.len(), 32);
    }

    #[test]
    fn expiry_boundary_is_inclusive_and_future_decisions_are_refused() {
        let r = rig(Side::Buy);
        assert!(check(&r, NOW + TTL_NS, ShortPolicy::Refuse).is_ok());
        assert_eq!(
            check(&r, NOW + TTL_NS + 1, ShortPolicy::Refuse),
            Err(VerifyError::Expired)
        );
        assert!(check(&r, NOW - DECIDED_AT_SKEW_NS, ShortPolicy::Refuse).is_ok());
        assert_eq!(
            check(&r, NOW - DECIDED_AT_SKEW_NS - 1, ShortPolicy::Refuse),
            Err(VerifyError::FromFuture)
        );
    }

    #[test]
    fn tampering_with_any_order_field_after_signing_is_detected() {
        type Mutation = Box<dyn Fn(&mut pb::OrderRequest)>;
        let mutations: Vec<(&str, Mutation)> = vec![
            ("symbol", Box::new(|o| o.symbol = "MSFT".into())),
            ("qty", Box::new(|o| o.quantity_nanos += 1)),
            ("price", Box::new(|o| o.limit_price_nanos += 1)),
            ("stop", Box::new(|o| o.stop_price_nanos = 1)),
            (
                "side",
                Box::new(|o| o.side = pb::OrderSide::OrderSell as i32),
            ),
            (
                "order_type",
                Box::new(|o| o.order_type = pb::OrderType::Market as i32),
            ),
            (
                "signal_id",
                Box::new(|o| o.signal_id = "11111111-6a61-4b0e-9a54-0e1e5d3f9a11".into()),
            ),
            (
                "order_id",
                Box::new(|o| o.order_id = "11111111-2a3b-4c55-8f7e-1a2b3c4d5e6f".into()),
            ),
        ];
        for (name, m) in mutations {
            let mut r = rig(Side::Buy);
            m(&mut r.order);
            assert!(
                check(&r, NOW + 1, ShortPolicy::Allow).is_err(),
                "tampered {name} must not verify"
            );
        }
    }

    #[test]
    fn tampering_with_attestation_fields_or_signature_is_detected() {
        let mut r = rig(Side::Buy);
        r.att.aegis_state_seq += 1;
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::DigestMismatch)
        );
        let mut r = rig(Side::Buy);
        r.att.limits_config_sha256 = "cd".repeat(32);
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::DigestMismatch)
        );
        let mut r = rig(Side::Buy);
        r.att.signature[0] ^= 1;
        r.order.hsm_signature = r.att.signature.clone();
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::BadSignature)
        );
        let mut r = rig(Side::Buy);
        r.att.expires_at_ns += 60_000_000_000; // stretched TTL
        r.order.attestation_expires_at_ns = r.att.expires_at_ns;
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::DigestMismatch)
        );
        let mut r = rig(Side::Buy);
        r.order.hsm_signature = vec![0; 64];
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::OrderAttestationMismatch("hsm_signature"))
        );
    }

    #[test]
    fn a_recomputed_digest_with_a_foreign_key_is_unknown_or_bad() {
        let mut r = rig(Side::Buy);
        r.att.key_id = "attacker".into();
        r.order.hsm_key_id = "attacker".into();
        assert!(check(&r, NOW + 1, ShortPolicy::Allow).is_err());
        let mut r = rig(Side::Buy);
        r.verifier = Ed25519Verifier::default();
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::UnknownKey)
        );
    }

    #[test]
    fn version_and_unknown_enums_are_refused() {
        let mut r = rig(Side::Buy);
        r.att.canonical_version = "afe-attest-v2".into();
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::UnsupportedVersion)
        );
        let mut r = rig(Side::Buy);
        r.order.side = 0;
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::UnsupportedOrder)
        );
        r.order.side = 77;
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::UnsupportedOrder)
        );
        let mut r = rig(Side::Buy);
        r.order.order_type = 0;
        assert_eq!(
            check(&r, NOW + 1, ShortPolicy::Allow),
            Err(VerifyError::UnsupportedOrder)
        );
    }

    #[test]
    fn side_mapping_sell_and_sell_short_share_order_sell_but_stay_distinguishable() {
        let sell = rig(Side::Sell);
        let short = rig(Side::SellShort);
        assert_eq!(sell.order.side, pb::OrderSide::OrderSell as i32);
        assert_eq!(short.order.side, pb::OrderSide::OrderSell as i32);
        assert_eq!(check(&sell, NOW + 1, ShortPolicy::Refuse), Ok(Side::Sell));
        assert_eq!(
            check(&short, NOW + 1, ShortPolicy::Allow),
            Ok(Side::SellShort)
        );
        assert_eq!(
            check(&short, NOW + 1, ShortPolicy::Refuse),
            Err(VerifyError::ShortNotPermitted)
        );
        // a SELL attestation cannot be replayed as a short (digest differs)
        assert_ne!(sell.att.payload_sha256, short.att.payload_sha256);
    }
}
