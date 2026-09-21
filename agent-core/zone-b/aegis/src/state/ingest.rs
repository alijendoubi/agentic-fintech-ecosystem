//! Reference-data ingestion (`PushReferenceData`). Pure function of
//! `(store, limits, now, request)`; the gRPC layer only authenticates and
//! bounds the batch.
//!
//! Fail closed, item by item. An item is stored only if ALL hold:
//!
//! * the symbol is on the C03 allowlist (nothing else is ever priced),
//! * mid and ADV are strictly positive nanos and the timestamp is > 0,
//! * `as_of` is not older than `max_ref_age_ms` (regime: `max_regime_age_ms`)
//!   and not more than `clock_skew_ms` in the future, both judged against
//!   Aegis's own clock, so a feed cannot backdate its way to "fresh",
//! * it is strictly newer than what is stored (monotonic per symbol / regime).
//!
//! Anything else is reported in `rejected` and NOT stored, so C08 / C18 keep
//! failing on the old value once it ages out. There is no default that accepts
//! stale data: the bounds are the same `timings` values C08 / C18 enforce.

use crate::controls::{RefPrice, RegimeView};
use crate::limits::LimitsConfig;
use crate::money::Nanos;
use crate::pb;
use crate::state::refdata::{MemoryReferenceData, RefDataError};

/// Largest batch of snapshots accepted in one push.
pub const MAX_SNAPSHOTS_PER_PUSH: usize = 512;
const MAX_SYMBOL_LEN: usize = 32;
const NANOS_PER_MS: i128 = 1_000_000;

pub const REJECT_INVALID: &str = "invalid";
pub const REJECT_STALE: &str = "stale";
pub const REJECT_FUTURE: &str = "future";
pub const REJECT_OUT_OF_ORDER: &str = "out_of_order";
pub const REJECT_UNKNOWN_SYMBOL: &str = "unknown_symbol";
pub const REJECT_CAPACITY: &str = "capacity";
const REGIME_KEY: &str = "regime";

/// Age of `as_of_ns` against `now_ns` classified against the freshness window.
fn check_age(
    as_of_ns: i64,
    now_ns: i64,
    max_age_ms: u64,
    skew_ms: u64,
) -> Result<(), &'static str> {
    let age = i128::from(now_ns) - i128::from(as_of_ns);
    if age > i128::from(max_age_ms) * NANOS_PER_MS {
        Err(REJECT_STALE)
    } else if age < -(i128::from(skew_ms) * NANOS_PER_MS) {
        Err(REJECT_FUTURE)
    } else {
        Ok(())
    }
}

fn store_error(e: RefDataError) -> &'static str {
    match e {
        RefDataError::OutOfOrder => REJECT_OUT_OF_ORDER,
        RefDataError::Invalid("too many symbols") => REJECT_CAPACITY,
        RefDataError::Invalid(_) => REJECT_INVALID,
    }
}

fn apply_snapshot(
    store: &MemoryReferenceData,
    cfg: &LimitsConfig,
    now_ns: i64,
    s: &pb::ReferenceSnapshot,
) -> Result<(), &'static str> {
    if s.symbol.is_empty() || s.symbol.len() > MAX_SYMBOL_LEN {
        return Err(REJECT_INVALID);
    }
    if !cfg.symbols.contains_key(&s.symbol) {
        return Err(REJECT_UNKNOWN_SYMBOL);
    }
    if s.mid_price_nanos <= 0 || s.adv_30d_nanos <= 0 || s.as_of_ns <= 0 {
        return Err(REJECT_INVALID);
    }
    let t = &cfg.timings;
    check_age(s.as_of_ns, now_ns, t.max_ref_age_ms, t.clock_skew_ms)?;
    store
        .set_price_monotonic(
            &s.symbol,
            RefPrice {
                mid: Nanos::new(s.mid_price_nanos),
                adv: Nanos::new(s.adv_30d_nanos),
                ingested_at_ns: s.as_of_ns,
                is_stale: s.is_stale,
            },
        )
        .map_err(store_error)
}

fn apply_regime(
    store: &MemoryReferenceData,
    cfg: &LimitsConfig,
    now_ns: i64,
    p: &pb::RegimeLabelPacket,
) -> Result<(), &'static str> {
    let label = pb::RegimeLabel::try_from(p.label).map_err(|_| REJECT_INVALID)?;
    if !p.confidence.is_finite() || !(0.0..=1.0).contains(&p.confidence) || p.timestamp_ns <= 0 {
        return Err(REJECT_INVALID);
    }
    let t = &cfg.timings;
    check_age(p.timestamp_ns, now_ns, t.max_regime_age_ms, t.clock_skew_ms)?;
    store
        .set_regime_monotonic(RegimeView {
            label,
            confidence: p.confidence,
            at_ns: p.timestamp_ns,
        })
        .map_err(store_error)
}

/// Apply one push. The caller has already authenticated the peer and checked
/// the batch bound.
pub fn ingest(
    store: &MemoryReferenceData,
    cfg: &LimitsConfig,
    now_ns: i64,
    req: &pb::PushReferenceDataRequest,
) -> pb::PushReferenceDataResponse {
    let mut resp = pb::PushReferenceDataResponse::default();
    for s in &req.snapshots {
        match apply_snapshot(store, cfg, now_ns, s) {
            Ok(()) => resp.applied_snapshots += 1,
            Err(reason) => resp.rejected.push(pb::ReferenceRejection {
                key: s.symbol.chars().take(MAX_SYMBOL_LEN).collect(),
                reason: reason.to_owned(),
            }),
        }
    }
    if let Some(p) = &req.regime {
        match apply_regime(store, cfg, now_ns, p) {
            Ok(()) => resp.regime_applied = true,
            Err(reason) => resp.rejected.push(pb::ReferenceRejection {
                key: REGIME_KEY.to_owned(),
                reason: reason.to_owned(),
            }),
        }
    }
    resp
}

#[cfg(test)]
mod tests;
