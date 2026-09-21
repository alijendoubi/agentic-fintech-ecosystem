//! Portfolio/statistical controls C15..C19.

use super::Snapshot;
use crate::domain::ValidatedSignal;
use crate::money::{bps_of_down, format_wide};
use crate::pb::{ControlResult, ReasonCode, RegimeLabel};

const NANOS_PER_MS: i128 = 1_000_000;

/// C15: block new risk once realised + unrealised loss reaches the limit.
pub(super) fn c15_drawdown(snap: &Snapshot<'_>) -> ControlResult {
    let bps = snap.limits.max_daily_drawdown_bps;
    let threshold = format!("loss < {bps} bps of day-start NAV");
    let Some(e) = snap.equity.filter(|e| e.day_start.get() > 0) else {
        return ControlResult::fail(
            "C15",
            true,
            ReasonCode::ReasonStateUnavailable,
            threshold,
            "day-start equity unavailable",
        );
    };
    let start = i128::from(e.day_start.get());
    let loss = start - i128::from(e.current.get());
    let Ok(limit) = bps_of_down(start, bps) else {
        return ControlResult::fail(
            "C15",
            true,
            ReasonCode::ReasonInternalError,
            threshold,
            "overflow",
        );
    };
    // A limit that rounds to zero would never trip: treat as breached.
    if limit == 0 || loss >= limit {
        ControlResult::fail(
            "C15",
            true,
            ReasonCode::ReasonDailyDrawdown,
            format!("loss < {}", format_wide(limit)),
            format!("loss {}", format_wide(loss)),
        )
    } else {
        ControlResult::pass(
            "C15",
            true,
            threshold,
            format!("loss {}", format_wide(loss.max(0))),
        )
    }
}

pub(super) fn c16_rate(snap: &Snapshot<'_>) -> ControlResult {
    let r = snap.rate;
    let threshold = "token available (global and per-symbol)";
    if r.global_ok && r.symbol_ok {
        ControlResult::pass("C16", true, threshold, "ok")
    } else {
        let observed = if r.global_ok {
            "per-symbol bucket empty"
        } else {
            "global bucket empty"
        };
        ControlResult::fail(
            "C16",
            true,
            ReasonCode::ReasonRateLimit,
            threshold,
            observed,
        )
    }
}

/// C17: abstain (hard reject, no human hold) below the omega threshold.
pub(super) fn c17_low_omega(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let min = snap.limits.omega_min;
    let threshold = format!("omega >= {min}");
    let observed = format!("{}", sig.omega);
    if sig.omega.is_finite() && sig.omega >= min {
        ControlResult::pass("C17", true, threshold, observed)
    } else {
        ControlResult::fail("C17", true, ReasonCode::ReasonLowOmega, threshold, observed)
    }
}

/// C18 (soft, spec 4.4): fires REGIME_LOW_CONFIDENCE if the confidence of
/// Aegis's OWN regime label is below `C_MIN`, and REGIME_MISMATCH if the
/// signal's regime is UNKNOWN, differs from Aegis's own label, or the label is
/// outside the strategy's validated set. No fallback to the signal's own
/// confidence (stricter than the spec's flagged fallback): a missing or stale
/// own label is a mismatch.
pub(super) fn c18_regime(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> Vec<ControlResult> {
    let cfg = &snap.limits.regime;
    let max_age = i128::from(snap.limits.timings.max_regime_age_ms) * NANOS_PER_MS;
    let threshold = format!(
        "confidence >= {} and regime in validated set",
        cfg.min_confidence
    );
    let fresh = snap
        .regime
        .filter(|r| i128::from(snap.now_ns) - i128::from(r.at_ns) <= max_age);
    let Some(own) = fresh else {
        return vec![ControlResult::fail(
            "C18",
            false,
            ReasonCode::ReasonRegimeMismatch,
            threshold,
            "own regime label missing or stale",
        )];
    };
    let mut out = Vec::new();
    if !(own.confidence.is_finite() && own.confidence >= cfg.min_confidence) {
        out.push(ControlResult::fail(
            "C18",
            false,
            ReasonCode::ReasonRegimeLowConfidence,
            format!("confidence >= {}", cfg.min_confidence),
            format!("{}", own.confidence),
        ));
    }
    let allowed = cfg.allowed.iter().any(|a| a == own.label.as_str_name());
    let unknown =
        sig.regime == RegimeLabel::RegimeUnknown || own.label == RegimeLabel::RegimeUnknown;
    if unknown || sig.regime != own.label || !allowed {
        out.push(ControlResult::fail(
            "C18",
            false,
            ReasonCode::ReasonRegimeMismatch,
            "signal regime == own regime, in validated set",
            format!(
                "signal {} / own {}",
                sig.regime.as_str_name(),
                own.label.as_str_name()
            ),
        ));
    }
    if out.is_empty() {
        out.push(ControlResult::pass(
            "C18",
            false,
            threshold,
            own.label.as_str_name(),
        ));
    }
    out
}

/// C19 (soft): orders in the top 5% of the trailing distribution are held;
/// cold start (too little history) holds every order.
pub(super) fn c19_unusual_size(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let reason = ReasonCode::ReasonUnusualOrderSize;
    let min = usize::try_from(snap.limits.timings.unusual_size_min_history).unwrap_or(usize::MAX);
    let h = snap.history;
    if h.count < min {
        return ControlResult::fail(
            "C19",
            false,
            reason,
            format!("history >= {min} orders"),
            format!("{} orders (cold start)", h.count),
        );
    }
    match h.p95 {
        Some(p95) if sig.qty <= p95 => ControlResult::pass(
            "C19",
            false,
            format!("qty <= P95 {p95}"),
            sig.qty.to_string(),
        ),
        Some(p95) => ControlResult::fail(
            "C19",
            false,
            reason,
            format!("qty <= P95 {p95}"),
            sig.qty.to_string(),
        ),
        None => ControlResult::fail("C19", false, reason, "P95 available", "unavailable"),
    }
}
