//! C08 reference-price freshness and C09 price collar (with the bounded
//! marketable limit for market orders, spec 4.5).

use super::Snapshot;
use crate::domain::ValidatedSignal;
use crate::money::{bps_of_down, format_wide, Nanos};
use crate::pb::{ControlResult, ReasonCode};

const NANOS_PER_MS: i128 = 1_000_000;

pub(super) fn c08_ref_freshness(_sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let reason = ReasonCode::ReasonStaleReferencePrice;
    let max_age = i128::from(snap.limits.timings.max_ref_age_ms) * NANOS_PER_MS;
    let skew = i128::from(snap.limits.timings.clock_skew_ms) * NANOS_PER_MS;
    let threshold = format!("age <= {} ms", snap.limits.timings.max_ref_age_ms);
    let Some(r) = snap.ref_price else {
        return ControlResult::fail("C08", true, reason, threshold, "no reference price");
    };
    let age = i128::from(snap.now_ns) - i128::from(r.ingested_at_ns);
    if r.is_stale {
        ControlResult::fail("C08", true, reason, threshold, "snapshot flagged stale")
    } else if r.mid.get() <= 0 {
        ControlResult::fail("C08", true, reason, threshold, "non-positive mid")
    } else if age > max_age {
        ControlResult::fail(
            "C08",
            true,
            reason,
            threshold,
            format!("age {} ms", age / NANOS_PER_MS),
        )
    } else if age < -skew {
        ControlResult::fail(
            "C08",
            true,
            reason,
            threshold,
            "snapshot timestamp in the future",
        )
    } else {
        ControlResult::pass(
            "C08",
            true,
            threshold,
            format!("age {} ms", age.max(0) / NANOS_PER_MS),
        )
    }
}

/// Bounded price for a market order: buy at `mid + collar`, sell at `mid - collar`.
fn marketable_limit(sig: &ValidatedSignal, mid: i128, allowed: i128) -> i128 {
    if sig.side.is_sell() {
        mid - allowed
    } else {
        mid + allowed
    }
}

/// Returns the C09 result and the price the order must carry (only on pass).
pub(super) fn c09_price_collar(
    sig: &ValidatedSignal,
    snap: &Snapshot<'_>,
) -> (ControlResult, Option<Nanos>) {
    let bps = snap.limits.price_collar_bps;
    let threshold = format!("+/-{bps} bps of mid");
    let unavailable = |why: &str| {
        (
            ControlResult::fail(
                "C09",
                true,
                ReasonCode::ReasonStateUnavailable,
                threshold.clone(),
                why,
            ),
            None,
        )
    };
    let Some(r) = snap.ref_price.filter(|r| r.mid.get() > 0) else {
        return unavailable("no usable reference price");
    };
    let mid = i128::from(r.mid.get());
    let Ok(allowed) = bps_of_down(mid, bps) else {
        return unavailable("collar arithmetic overflow");
    };
    let price = match sig.limit_price {
        Some(p) => i128::from(p.get()),
        None => marketable_limit(sig, mid, allowed),
    };
    let deviation = (price - mid).abs();
    let in_collar = deviation <= allowed;
    let as_nanos = i64::try_from(price).ok().filter(|p| *p > 0).map(Nanos::new);
    match (in_collar, as_nanos) {
        (true, Some(p)) => (
            ControlResult::pass(
                "C09",
                true,
                threshold,
                format!("deviation {}", format_wide(deviation)),
            ),
            Some(p),
        ),
        (true, None) => unavailable("bounded price not representable"),
        (false, _) => (
            ControlResult::fail(
                "C09",
                true,
                ReasonCode::ReasonPriceCollar,
                format!("deviation <= {}", format_wide(allowed)),
                format!("deviation {}", format_wide(deviation)),
            ),
            None,
        ),
    }
}
