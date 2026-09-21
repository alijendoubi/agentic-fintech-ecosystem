//! Test fixture: a baseline that APPROVES, so each test perturbs one input.

use super::*;
use crate::domain::Side;
use crate::limits::{test_limits_json, Limits};

/// 2026-09-21T14:13:20Z, a Monday, inside the test session (13:30-20:00 UTC).
pub const NOW_NS: i64 = 1_790_000_000 * 1_000_000_000;
pub const SHARE: i64 = 1_000_000_000;

#[derive(Debug)]
pub struct Fixture {
    pub limits: LimitsConfig,
    pub sig: ValidatedSignal,
    pub mode: Mode,
    pub now_ns: i64,
    pub kill: Option<KillSwitchLevel>,
    pub replay: ReplayVerdict,
    pub ref_price: Option<RefPrice>,
    pub regime: Option<RegimeView>,
    pub exposure: Option<ExposureView>,
    pub equity: Option<EquityView>,
    pub rate: RateView,
    pub history: SizeHistory,
}

impl Fixture {
    pub fn new() -> Fixture {
        let limits = Limits::from_bytes(test_limits_json().as_bytes())
            .expect("test limits")
            .config;
        Fixture {
            limits,
            sig: ValidatedSignal {
                signal_id: "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11".into(),
                symbol: "AAPL".into(),
                created_at_ns: NOW_NS - 100_000_000,
                valid_until_ns: NOW_NS + 4_000_000_000,
                side: Side::Buy,
                qty: Nanos::new(10 * SHARE),
                limit_price: Some(Nanos::new(150 * SHARE)),
                omega: 0.8,
                regime: RegimeLabel::TrendingBull,
                regime_confidence: 0.9,
                strategy_id: "AFE-STRATEGY-001".into(),
            },
            mode: Mode::Submit,
            now_ns: NOW_NS,
            kill: Some(KillSwitchLevel::KillLevelNormal),
            replay: ReplayVerdict::New,
            ref_price: Some(RefPrice {
                mid: Nanos::new(150 * SHARE),
                adv: Nanos::new(1_000_000 * SHARE),
                ingested_at_ns: NOW_NS - 10_000_000,
                is_stale: false,
            }),
            regime: Some(RegimeView {
                label: RegimeLabel::TrendingBull,
                confidence: 0.9,
                at_ns: NOW_NS - 1_000_000_000,
            }),
            exposure: Some(ExposureView {
                position: Nanos::ZERO,
                pending_buy: 0,
                pending_sell: 0,
                others_gross: Some(0),
            }),
            equity: Some(EquityView {
                day_start: Nanos::new(1_000_000 * SHARE),
                current: Nanos::new(1_000_000 * SHARE),
            }),
            rate: RateView {
                global_ok: true,
                symbol_ok: true,
            },
            history: SizeHistory {
                count: 30,
                p95: Some(Nanos::new(20 * SHARE)),
            },
        }
    }

    pub fn snap(&self) -> Snapshot<'_> {
        Snapshot {
            limits: &self.limits,
            mode: self.mode,
            now_ns: self.now_ns,
            kill: self.kill,
            replay: self.replay,
            ref_price: self.ref_price,
            regime: self.regime,
            exposure: self.exposure,
            equity: self.equity,
            rate: self.rate,
            history: self.history,
        }
    }

    pub fn run(&self) -> Evaluation {
        evaluate(&self.sig, &self.snap())
    }
}

/// Look up the (first) result for a control id.
pub fn result<'a>(e: &'a Evaluation, id: &str) -> &'a ControlResult {
    e.results
        .iter()
        .find(|r| r.control_id == id)
        .unwrap_or_else(|| panic!("no result for {id}"))
}
