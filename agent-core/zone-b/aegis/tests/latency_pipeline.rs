//! Criterion-free timing test for the pure control pipeline (spec section 8
//! budgets C08-C19 at 3 ms p99 and the whole Aegis path at < 50 ms).
//!
//! Run in release for meaningful numbers:
//!   cargo test --release --test latency_pipeline -- --nocapture
//! In debug builds the assertion uses the full 50 ms budget so the test is not
//! flaky; in release it asserts the tighter 3 ms allocation for controls.

use std::time::Instant;

use aegis::controls::{
    evaluate, EquityView, ExposureView, RateView, RefPrice, RegimeView, ReplayVerdict, SizeHistory,
    Snapshot,
};
use aegis::domain::{Mode, Side, ValidatedSignal};
use aegis::limits::Limits;
use aegis::money::Nanos;
use aegis::pb::{DecisionStatus, KillSwitchLevel, RegimeLabel};

const SHARE: i64 = 1_000_000_000;
const NOW_NS: i64 = 1_790_000_000 * 1_000_000_000;
const ITERATIONS: usize = 20_000;

const LIMITS: &str = r#"{
  "symbols": {"AAPL": {"max_order_qty_nanos": 100000000000, "max_order_notional_nanos": 50000000000000, "max_position_nanos": 500000000000}},
  "short_selling_enabled": false, "accept_legacy_double_fields": false,
  "session": {"weekdays_utc": [1,2,3,4,5], "start_minute_utc": 810, "end_minute_utc": 1200},
  "price_collar_bps": 150, "max_order_adv_bps": 50, "max_gross_exposure_nanos": 1000000000000000,
  "max_daily_drawdown_bps": 200,
  "rate_global": {"capacity": 20, "refill_per_sec": 10}, "rate_per_symbol": {"capacity": 5, "refill_per_sec": 2},
  "omega_min": 0.55, "regime": {"min_confidence": 0.6, "allowed": ["TRENDING_BULL"]},
  "hold_requires_second_approver": false, "hold_requires_cooling_period": false,
  "hold_max_distress_score": 0.8, "audit_mandatory": true }"#;

#[test]
fn pipeline_latency_is_far_below_budget() {
    let limits = Limits::from_bytes(LIMITS.as_bytes()).expect("test limits");
    let sig = ValidatedSignal {
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
    };
    let snap = Snapshot {
        limits: &limits.config,
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
    };

    let mut samples_us = Vec::with_capacity(ITERATIONS);
    for _ in 0..ITERATIONS {
        let t = Instant::now();
        let e = evaluate(&sig, &snap);
        samples_us.push(t.elapsed().as_nanos());
        assert_eq!(e.decision, DecisionStatus::DecisionApproved);
    }
    samples_us.sort_unstable();
    let p50 = samples_us[ITERATIONS / 2];
    let p99 = samples_us[ITERATIONS * 99 / 100];
    let max = samples_us[ITERATIONS - 1];
    println!(
        "pipeline evaluate() over {ITERATIONS} runs: p50={p50} ns, p99={p99} ns, max={max} ns (release={})",
        !cfg!(debug_assertions)
    );
    let budget_ns: u128 = if cfg!(debug_assertions) {
        50_000_000
    } else {
        3_000_000
    };
    assert!(
        p99 < budget_ns,
        "p99 {p99} ns exceeds budget {budget_ns} ns"
    );
}
