//! Pre-trade controls C01..C19 (spec section 4).
//!
//! Each control is a PURE function of `(&ValidatedSignal, &Snapshot)` returning
//! a `ControlResult`. Nothing here performs I/O, reads a clock or mutates
//! state: the engine assembles a [`Snapshot`] from Aegis-owned state and then
//! [`evaluate`] runs the pipeline.
//!
//! Pipeline (spec 4.2):
//! 1. C01, C03..C07 in order; the first failure returns immediately.
//! 2. C08..C19 are ALL evaluated so the audit record lists every violation.
//! 3. Any hard failure => REJECTED; else any soft failure => HELD_FOR_HUMAN;
//!    else APPROVED. The absence of a data input is a failure, never a pass.

mod gate;
mod market;
mod risk;
mod sizing;

pub use gate::session_contains;

use crate::domain::{Mode, ValidatedSignal};
use crate::limits::LimitsConfig;
use crate::money::Nanos;
use crate::pb::{
    ControlResult, DecisionStatus, KillSwitchLevel, ReasonCode, RegimeLabel, SignalStatus,
};

/// Aegis-owned reference data for one symbol (never taken from the signal).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RefPrice {
    pub mid: Nanos,
    /// 30-day average daily volume in share-nanos.
    pub adv: Nanos,
    pub ingested_at_ns: i64,
    pub is_stale: bool,
}

/// Aegis's own latest regime label for the strategy universe.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RegimeView {
    pub label: RegimeLabel,
    pub confidence: f64,
    pub at_ns: i64,
}

/// Worst-case exposure inputs for the signal's symbol, in share-nanos.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ExposureView {
    /// Confirmed position (signed).
    pub position: Nanos,
    /// Sum of approved-but-unfilled BUY quantity.
    pub pending_buy: i128,
    /// Sum of approved-but-unfilled SELL quantity.
    pub pending_sell: i128,
    /// Worst-case gross exposure (nanos of currency) of every OTHER symbol,
    /// or `None` if any mark price needed for it is unavailable.
    pub others_gross: Option<i128>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct EquityView {
    pub day_start: Nanos,
    pub current: Nanos,
}

/// Outcome of the token-bucket check performed by the engine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RateView {
    pub global_ok: bool,
    pub symbol_ok: bool,
}

/// Trailing order-size distribution for the symbol (C19).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SizeHistory {
    pub count: usize,
    pub p95: Option<Nanos>,
}

/// What the replay store said about this signal id (C07).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplayVerdict {
    New,
    PayloadMismatch,
    Duplicate,
    StoreUnavailable,
}

/// Everything the controls may look at. `None` means "unavailable" and always
/// fails the control that needs it.
#[derive(Debug, Clone)]
pub struct Snapshot<'a> {
    pub limits: &'a LimitsConfig,
    pub mode: Mode,
    pub now_ns: i64,
    /// `None` = kill-switch state missing/undecodable, treated as HARD.
    pub kill: Option<KillSwitchLevel>,
    pub replay: ReplayVerdict,
    pub ref_price: Option<RefPrice>,
    pub regime: Option<RegimeView>,
    pub exposure: Option<ExposureView>,
    pub equity: Option<EquityView>,
    pub rate: RateView,
    pub history: SizeHistory,
}

/// Result of running the whole pipeline.
#[derive(Debug, Clone)]
pub struct Evaluation {
    pub results: Vec<ControlResult>,
    pub decision: DecisionStatus,
    pub signal_status: SignalStatus,
    pub reasons: Vec<ReasonCode>,
    /// Price the order must carry: the signal's limit, or the bounded
    /// marketable limit for a market order (spec 4.5). `None` unless C09 passed.
    pub order_price: Option<Nanos>,
}

fn phase_one(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> Vec<ControlResult> {
    let checks: [fn(&ValidatedSignal, &Snapshot<'_>) -> ControlResult; 6] = [
        gate::c01_kill_switch,
        gate::c03_symbol_allowlist,
        gate::c04_short_sale,
        gate::c05_session,
        gate::c06_expiry,
        gate::c07_replay,
    ];
    let mut out = Vec::with_capacity(checks.len());
    for check in checks {
        let r = check(sig, snap);
        let stop = !r.passed && r.is_hard;
        out.push(r);
        if stop {
            break;
        }
    }
    out
}

fn phase_two(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> (Vec<ControlResult>, Option<Nanos>) {
    let mut out = vec![market::c08_ref_freshness(sig, snap)];
    let (c09, order_price) = market::c09_price_collar(sig, snap);
    out.push(c09);
    out.push(sizing::c10_max_qty(sig, snap));
    out.push(sizing::c11_max_notional(sig, snap, order_price));
    out.push(sizing::c12_adv(sig, snap));
    out.push(sizing::c13_position_limit(sig, snap));
    out.push(sizing::c14_gross_exposure(sig, snap));
    out.push(risk::c15_drawdown(snap));
    out.push(risk::c16_rate(snap));
    out.push(risk::c17_low_omega(sig, snap));
    if snap.mode == Mode::Submit {
        out.extend(risk::c18_regime(sig, snap));
        out.push(risk::c19_unusual_size(sig, snap));
    }
    (out, order_price)
}

/// Map failed controls to a decision (spec 4.2 items 3 and 4).
fn decide(results: &[ControlResult]) -> (DecisionStatus, SignalStatus) {
    let failed = |hard: bool| {
        results
            .iter()
            .filter(move |r| !r.passed && r.is_hard == hard)
    };
    let hard: Vec<&ControlResult> = failed(true).collect();
    if !hard.is_empty() {
        let only = |id: &str| hard.iter().all(|r| r.control_id == id);
        let status = if only("C17") {
            SignalStatus::SignalAbstain
        } else if only("C06") {
            SignalStatus::SignalExpired
        } else {
            SignalStatus::SignalRejectedHardBlock
        };
        return (DecisionStatus::DecisionRejected, status);
    }
    if failed(false).next().is_some() {
        return (
            DecisionStatus::DecisionHeldForHuman,
            SignalStatus::SignalSoftBlockPending,
        );
    }
    (
        DecisionStatus::DecisionApproved,
        SignalStatus::SignalApproved,
    )
}

/// Run the full pre-trade pipeline for a validated signal.
pub fn evaluate(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> Evaluation {
    let mut results = phase_one(sig, snap);
    let mut order_price = None;
    if results.iter().all(|r| r.passed || !r.is_hard) {
        let (rest, price) = phase_two(sig, snap);
        results.extend(rest);
        order_price = price;
    }
    // Defence in depth: an approval must carry a bounded price.
    if order_price.is_none() && results.iter().all(|r| r.passed) {
        results.push(
            ControlResult::fail(
                "C09",
                true,
                ReasonCode::ReasonInternalError,
                "bounded order price",
                "missing",
            )
            .with_detail("pipeline produced no order price"),
        );
    }
    let (decision, signal_status) = decide(&results);
    let mut reasons: Vec<ReasonCode> = Vec::new();
    for r in results.iter().filter(|r| !r.passed) {
        let code = ReasonCode::try_from(r.reason).unwrap_or(ReasonCode::ReasonInternalError);
        if !reasons.contains(&code) {
            reasons.push(code);
        }
    }
    Evaluation {
        results,
        decision,
        signal_status,
        reasons,
        order_price,
    }
}

#[cfg(test)]
pub(crate) mod testutil;

#[cfg(test)]
mod tests;
