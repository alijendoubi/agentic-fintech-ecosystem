//! Turns an approved evaluation into a signed `OrderRequest` + `Attestation`.
//!
//! Side mapping (README "Attestation contract"): the attested text carries the
//! SIGNAL vocabulary (`BUY|SELL|SELL_SHORT`); the wire `OrderRequest.side` is
//! `ORDER_BUY` for BUY and `ORDER_SELL` for SELL and SELL_SHORT (OrderSide has
//! no short value). A verifier that sees `ORDER_SELL` tries `SELL` then
//! `SELL_SHORT` (see `verify`).

use super::canonical::{AttestationFields, CANONICAL_VERSION};
use super::{SignError, Signer};
use crate::domain::{Side, ValidatedSignal};
use crate::money::Nanos;
use crate::pb;

/// Inputs fixed by Aegis at decision time.
pub struct AttestParams<'a> {
    pub signal: &'a ValidatedSignal,
    /// Bounded price from C09 (limit or marketable limit).
    pub price: Nanos,
    pub decided_at_ns: i64,
    pub ttl_ns: i64,
    pub state_seq: u64,
    pub limits_sha256: &'a str,
}

pub fn order_side(side: Side) -> pb::OrderSide {
    match side {
        Side::Buy => pb::OrderSide::OrderBuy,
        Side::Sell | Side::SellShort => pb::OrderSide::OrderSell,
    }
}

/// Order type name as attested. Aegis only ever emits bounded LIMIT orders
/// (market orders are converted, spec 4.5).
pub const ORDER_TYPE_LIMIT: &str = "LIMIT";

/// Sign and assemble the order. Any failure means "no attestation".
pub fn attest_order(
    signer: &dyn Signer,
    p: &AttestParams<'_>,
) -> Result<(pb::OrderRequest, pb::Attestation), SignError> {
    let expires_at_ns = p
        .decided_at_ns
        .checked_add(p.ttl_ns)
        .ok_or_else(|| SignError::Canonical("expiry overflow".into()))?;
    // The attested text (spec section 7) binds `signal_id`, not `order_id`, so
    // the client order id IS the signal id: one signal, one order, and the
    // broker's idempotency key is covered by the signature.
    let order_id = p.signal.signal_id.clone();
    let fields = AttestationFields {
        signal_id: p.signal.signal_id.clone(),
        symbol: p.signal.symbol.clone(),
        side: p.signal.side,
        order_type: ORDER_TYPE_LIMIT.to_owned(),
        qty_nanos: p.signal.qty.get(),
        limit_price_nanos: p.price.get(),
        stop_price_nanos: 0,
        decided_at_ns: p.decided_at_ns,
        expires_at_ns,
        aegis_state_seq: p.state_seq,
        limits_config_sha256: p.limits_sha256.to_owned(),
        key_id: signer.key_id().to_owned(),
    };
    let digest = fields.digest()?;
    let signature = signer.sign(&digest)?;
    let attestation = pb::Attestation {
        canonical_version: CANONICAL_VERSION.to_owned(),
        payload_sha256: digest.to_vec(),
        signature: signature.clone(),
        key_id: signer.key_id().to_owned(),
        decided_at_ns: p.decided_at_ns,
        expires_at_ns,
        aegis_state_seq: p.state_seq,
        limits_config_sha256: p.limits_sha256.to_owned(),
    };
    let order = pb::OrderRequest {
        order_id,
        signal_id: p.signal.signal_id.clone(),
        symbol: p.signal.symbol.clone(),
        created_at_ns: p.decided_at_ns,
        side: order_side(p.signal.side) as i32,
        order_type: pb::OrderType::Limit as i32,
        hsm_signature: signature,
        hsm_key_id: signer.key_id().to_owned(),
        algo: pb::ExecAlgo::Direct as i32,
        status: pb::OrderStatus::OrderPending as i32,
        quantity_nanos: p.signal.qty.get(),
        limit_price_nanos: p.price.get(),
        stop_price_nanos: 0,
        attestation_expires_at_ns: expires_at_ns,
        ..pb::OrderRequest::default()
    };
    Ok((order, attestation))
}
