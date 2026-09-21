//! Size and exposure controls C10..C14. All arithmetic is checked i128;
//! overflow fails the control with `REASON_INTERNAL_ERROR` (spec 3).

use super::{ExposureView, Snapshot};
use crate::domain::ValidatedSignal;
use crate::limits::SymbolLimits;
use crate::money::{bps_of_down, format_wide, mul_up, Nanos};
use crate::pb::{ControlResult, ReasonCode};

fn unavailable(id: &str, threshold: String, why: &str) -> ControlResult {
    ControlResult::fail(id, true, ReasonCode::ReasonStateUnavailable, threshold, why)
}

fn overflow(id: &str, threshold: String) -> ControlResult {
    ControlResult::fail(
        id,
        true,
        ReasonCode::ReasonInternalError,
        threshold,
        "arithmetic overflow",
    )
}

fn symbol_limits<'a>(sig: &ValidatedSignal, snap: &'a Snapshot<'_>) -> Option<&'a SymbolLimits> {
    snap.limits.symbols.get(&sig.symbol)
}

pub(super) fn c10_max_qty(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let Some(l) = symbol_limits(sig, snap) else {
        return unavailable("C10", "per-symbol cap".into(), "no limits for symbol");
    };
    let threshold = format!("qty <= {}", Nanos::new(l.max_order_qty_nanos));
    if sig.qty.get() <= l.max_order_qty_nanos {
        ControlResult::pass("C10", true, threshold, sig.qty.to_string())
    } else {
        ControlResult::fail(
            "C10",
            true,
            ReasonCode::ReasonMaxOrderQuantity,
            threshold,
            sig.qty.to_string(),
        )
    }
}

pub(super) fn c11_max_notional(
    sig: &ValidatedSignal,
    snap: &Snapshot<'_>,
    order_price: Option<Nanos>,
) -> ControlResult {
    let Some(l) = symbol_limits(sig, snap) else {
        return unavailable("C11", "per-symbol cap".into(), "no limits for symbol");
    };
    let threshold = format!("notional <= {}", Nanos::new(l.max_order_notional_nanos));
    let Some(price) = order_price else {
        return unavailable("C11", threshold, "no bounded order price");
    };
    let Ok(notional) = mul_up(i128::from(price.get()), i128::from(sig.qty.get())) else {
        return overflow("C11", threshold);
    };
    let observed = format_wide(notional);
    if notional <= i128::from(l.max_order_notional_nanos) {
        ControlResult::pass("C11", true, threshold, observed)
    } else {
        ControlResult::fail(
            "C11",
            true,
            ReasonCode::ReasonMaxOrderNotional,
            threshold,
            observed,
        )
    }
}

pub(super) fn c12_adv(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let bps = snap.limits.max_order_adv_bps;
    let threshold = format!("qty <= {bps} bps of adv_30d");
    let Some(r) = snap.ref_price.filter(|r| r.adv.get() > 0) else {
        return unavailable("C12", threshold, "no ADV available");
    };
    let Ok(allowed) = bps_of_down(i128::from(r.adv.get()), bps) else {
        return overflow("C12", threshold);
    };
    let qty = i128::from(sig.qty.get());
    if qty <= allowed {
        ControlResult::pass("C12", true, threshold, sig.qty.to_string())
    } else {
        ControlResult::fail(
            "C12",
            true,
            ReasonCode::ReasonOrderSizeAdv,
            format!("qty <= {}", format_wide(allowed)),
            sig.qty.to_string(),
        )
    }
}

/// Largest absolute position reachable if every pending order and this order
/// fill in the worst direction. `None` on overflow.
pub(super) fn worst_case_position(sig: &ValidatedSignal, x: &ExposureView) -> Option<i128> {
    let pos = i128::from(x.position.get());
    let qty = i128::from(sig.qty.get());
    let (buy_now, sell_now) = if sig.side.is_sell() {
        (0, qty)
    } else {
        (qty, 0)
    };
    let long = pos.checked_add(x.pending_buy)?.checked_add(buy_now)?;
    let short = pos.checked_sub(x.pending_sell)?.checked_sub(sell_now)?;
    Some(long.abs().max(short.abs()))
}

pub(super) fn c13_position_limit(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let Some(l) = symbol_limits(sig, snap) else {
        return unavailable("C13", "per-symbol cap".into(), "no limits for symbol");
    };
    let threshold = format!("|position| <= {}", Nanos::new(l.max_position_nanos));
    let Some(x) = snap.exposure else {
        return unavailable("C13", threshold, "position unavailable");
    };
    let Some(worst) = worst_case_position(sig, &x) else {
        return overflow("C13", threshold);
    };
    let observed = format_wide(worst);
    if worst <= i128::from(l.max_position_nanos) {
        ControlResult::pass("C13", true, threshold, observed)
    } else {
        ControlResult::fail(
            "C13",
            true,
            ReasonCode::ReasonPositionLimitSymbol,
            threshold,
            observed,
        )
    }
}

pub(super) fn c14_gross_exposure(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let max = snap.limits.max_gross_exposure_nanos;
    let threshold = format!("gross <= {}", Nanos::new(max));
    let (Some(x), Some(r)) = (snap.exposure, snap.ref_price) else {
        return unavailable("C14", threshold, "exposure or mark unavailable");
    };
    let (Some(others), Some(worst)) = (x.others_gross, worst_case_position(sig, &x)) else {
        return unavailable("C14", threshold, "gross exposure unavailable");
    };
    let Some(total) = mul_up(worst, i128::from(r.mid.get()))
        .ok()
        .and_then(|own| own.checked_add(others))
    else {
        return overflow("C14", threshold);
    };
    let observed = format_wide(total);
    if total <= i128::from(max) {
        ControlResult::pass("C14", true, threshold, observed)
    } else {
        ControlResult::fail(
            "C14",
            true,
            ReasonCode::ReasonGrossExposureLimit,
            threshold,
            observed,
        )
    }
}
