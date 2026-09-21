//! Per-control tests. Test names carry the control id (spec section 12).

use super::testutil::*;
use super::*;
use crate::domain::Side;
use proptest::prelude::*;

fn with(f: impl FnOnce(&mut Fixture)) -> Evaluation {
    let mut fx = Fixture::new();
    f(&mut fx);
    fx.run()
}

fn assert_rejects(e: &Evaluation, id: &str, reason: ReasonCode) {
    assert_eq!(
        e.decision,
        DecisionStatus::DecisionRejected,
        "{:?}",
        e.reasons
    );
    let r = result(e, id);
    assert!(!r.passed, "{id} should fail");
    assert_eq!(r.reason, reason as i32, "{id} reason");
    assert!(e.reasons.contains(&reason));
}

#[test]
fn baseline_is_approved_with_price_and_no_failures() {
    let e = Fixture::new().run();
    assert_eq!(e.decision, DecisionStatus::DecisionApproved);
    assert_eq!(e.signal_status, SignalStatus::SignalApproved);
    assert!(e.results.iter().all(|r| r.passed));
    assert!(e.reasons.is_empty());
    assert_eq!(e.order_price, Some(Nanos::new(150 * SHARE)));
    for id in [
        "C01", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12", "C13", "C14",
        "C15", "C16", "C17", "C18", "C19",
    ] {
        result(&e, id);
    }
}

// ---- C01 ----
#[test]
fn c01_kill_levels_normal_soft_logic_dead_hard_physical() {
    use KillSwitchLevel::*;
    for (lvl, expect_ok) in [
        (KillLevelNormal, true),
        (KillLevelSoft, false),
        (KillLevelLogic, false),
        (KillLevelDeadMans, false),
        (KillLevelHard, false),
        (KillLevelPhysical, false),
    ] {
        let e = with(|f| f.kill = Some(lvl));
        assert_eq!(
            e.decision == DecisionStatus::DecisionApproved,
            expect_ok,
            "{lvl:?}"
        );
    }
}

#[test]
fn c01_missing_kill_state_is_treated_as_hard_never_normal() {
    let e = with(|f| f.kill = None);
    assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
    assert_eq!(result(&e, "C01").observed, "UNAVAILABLE");
}

fn long_position(f: &mut Fixture, qty: i64) {
    f.exposure = Some(ExposureView {
        position: Nanos::new(qty * SHARE),
        pending_buy: 0,
        pending_sell: 0,
        others_gross: Some(0),
    });
}

#[test]
fn c01_soft_allows_only_strictly_risk_reducing_orders() {
    // long 50, sell 10 => reduces
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelSoft);
        long_position(f, 50);
        f.sig.side = Side::Sell;
    });
    assert!(result(&e, "C01").passed);
    // long 50, buy 10 => increases
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelSoft);
        long_position(f, 50);
    });
    assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
    // flat, sell => opens a short => increases
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelSoft);
        f.sig.side = Side::Sell;
    });
    assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
    // unknown position => treated as risk-increasing
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelSoft);
        f.exposure = None;
        f.sig.side = Side::Sell;
    });
    assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
    // pending orders in the symbol => not provably reducing
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelSoft);
        long_position(f, 50);
        f.exposure = f.exposure.map(|mut x| {
            x.pending_buy = 1;
            x
        });
        f.sig.side = Side::Sell;
    });
    assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
}

#[test]
fn c01_logic_holds_risk_reducing_for_human_and_rejects_the_rest() {
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelLogic);
        long_position(f, 50);
        f.sig.side = Side::Sell;
    });
    assert_eq!(e.decision, DecisionStatus::DecisionHeldForHuman);
    let e = with(|f| f.kill = Some(KillSwitchLevel::KillLevelLogic));
    assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
}

#[test]
fn c01_logic_release_of_risk_reducing_order_is_approved_only_in_release_mode() {
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelLogic);
        f.mode = Mode::Release;
        long_position(f, 50);
        f.sig.side = Side::Sell;
    });
    assert_eq!(e.decision, DecisionStatus::DecisionApproved);
    // a risk-increasing order can never be released at LOGIC
    let e = with(|f| {
        f.kill = Some(KillSwitchLevel::KillLevelLogic);
        f.mode = Mode::Release;
    });
    assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
    // nothing is released at DEAD_MANS or above
    for lvl in [
        KillSwitchLevel::KillLevelDeadMans,
        KillSwitchLevel::KillLevelHard,
        KillSwitchLevel::KillLevelPhysical,
    ] {
        let e = with(|f| {
            f.kill = Some(lvl);
            f.mode = Mode::Release;
            long_position(f, 50);
            f.sig.side = Side::Sell;
        });
        assert_rejects(&e, "C01", ReasonCode::ReasonKillSwitchActive);
    }
}

#[test]
fn c01_failure_returns_immediately_without_phase_two() {
    let e = with(|f| f.kill = Some(KillSwitchLevel::KillLevelHard));
    assert_eq!(e.results.len(), 1);
}

// ---- C03 / C04 / C05 ----
#[test]
fn c03_symbol_outside_universe_is_rejected() {
    let e = with(|f| f.sig.symbol = "TSLA".into());
    assert_rejects(&e, "C03", ReasonCode::ReasonSymbolNotAllowed);
}

#[test]
fn c04_short_sale_rejected_unless_enabled() {
    let e = with(|f| f.sig.side = Side::SellShort);
    assert_rejects(&e, "C04", ReasonCode::ReasonShortSellingDisabled);
    let e = with(|f| {
        f.sig.side = Side::SellShort;
        f.limits.short_selling_enabled = true;
        long_position(f, 0);
    });
    assert!(result(&e, "C04").passed);
}

#[test]
fn c05_session_boundaries_and_weekend() {
    // start 13:30 (810) inclusive, end 20:00 (1200) exclusive; NOW is 14:13:20 Monday
    let minute_ns = 60_000_000_000_i64;
    let day_start = NOW_NS - (14 * 60 + 13) * minute_ns - 20_000_000_000;
    let at = |min: i64| day_start + min * minute_ns;
    for (t, ok) in [
        (at(809), false),
        (at(810), true),
        (at(1199), true),
        (at(1200), false),
    ] {
        let e = with(|f| {
            f.now_ns = t;
            f.sig.created_at_ns = t - 100_000_000;
            f.sig.valid_until_ns = t + 4_000_000_000;
            f.ref_price = f.ref_price.map(|mut r| {
                r.ingested_at_ns = t - 10_000_000;
                r
            });
            f.regime = f.regime.map(|mut r| {
                r.at_ns = t - 1_000_000_000;
                r
            });
        });
        assert_eq!(result(&e, "C05").passed, ok, "minute of day at {t}");
    }
    let saturday = NOW_NS + 5 * 86_400_000_000_000;
    let e = with(|f| {
        f.now_ns = saturday;
        f.sig.created_at_ns = saturday - 100_000_000;
        f.sig.valid_until_ns = saturday + 4_000_000_000;
    });
    assert_rejects(&e, "C05", ReasonCode::ReasonOutsideTradingSession);
}

// ---- C06 ----
#[test]
fn c06_expiry_boundaries() {
    let e = with(|f| f.sig.valid_until_ns = NOW_NS); // exactly now: not yet expired
    assert!(result(&e, "C06").passed);
    let e = with(|f| f.sig.valid_until_ns = NOW_NS - 1);
    assert_rejects(&e, "C06", ReasonCode::ReasonSignalExpired);
    assert_eq!(e.signal_status, SignalStatus::SignalExpired);
    let e = with(|f| f.sig.valid_until_ns = 0);
    assert_rejects(&e, "C06", ReasonCode::ReasonSignalExpired);
}

#[test]
fn c06_future_and_age_boundaries() {
    let skew = 250_000_000;
    let e = with(|f| f.sig.created_at_ns = NOW_NS + skew);
    assert!(result(&e, "C06").passed);
    let e = with(|f| f.sig.created_at_ns = NOW_NS + skew + 1);
    assert_rejects(&e, "C06", ReasonCode::ReasonSignalFromFuture);
    let max_age = 5_000_000_000;
    let e = with(|f| {
        f.sig.created_at_ns = NOW_NS - max_age;
        f.sig.valid_until_ns = NOW_NS + 1_000_000_000;
    });
    assert!(result(&e, "C06").passed);
    let e = with(|f| {
        f.sig.created_at_ns = NOW_NS - max_age - 1;
        f.sig.valid_until_ns = NOW_NS + 1_000_000_000;
    });
    assert_rejects(&e, "C06", ReasonCode::ReasonSignalExpired);
}

// ---- C07 ----
#[test]
fn c07_replay_verdicts() {
    let e = with(|f| f.replay = ReplayVerdict::Duplicate);
    assert_rejects(&e, "C07", ReasonCode::ReasonDuplicateSignal);
    let e = with(|f| f.replay = ReplayVerdict::PayloadMismatch);
    assert_rejects(&e, "C07", ReasonCode::ReasonReplayPayloadMismatch);
    let e = with(|f| f.replay = ReplayVerdict::StoreUnavailable);
    assert_rejects(&e, "C07", ReasonCode::ReasonStateUnavailable);
}

// ---- C08 / C09 ----
#[test]
fn c08_reference_price_freshness() {
    let e = with(|f| f.ref_price = None);
    assert_rejects(&e, "C08", ReasonCode::ReasonStaleReferencePrice);
    let e = with(|f| {
        f.ref_price = f.ref_price.map(|mut r| {
            r.is_stale = true;
            r
        })
    });
    assert_rejects(&e, "C08", ReasonCode::ReasonStaleReferencePrice);
    let max_ref = 1_000_000_000;
    let e = with(|f| {
        f.ref_price = f.ref_price.map(|mut r| {
            r.ingested_at_ns = NOW_NS - max_ref;
            r
        })
    });
    assert!(result(&e, "C08").passed);
    let e = with(|f| {
        f.ref_price = f.ref_price.map(|mut r| {
            r.ingested_at_ns = NOW_NS - max_ref - 1;
            r
        })
    });
    assert_rejects(&e, "C08", ReasonCode::ReasonStaleReferencePrice);
    let e = with(|f| {
        f.ref_price = f.ref_price.map(|mut r| {
            r.ingested_at_ns = NOW_NS + 10_000_000_000;
            r
        })
    });
    assert_rejects(&e, "C08", ReasonCode::ReasonStaleReferencePrice);
    let e = with(|f| {
        f.ref_price = f.ref_price.map(|mut r| {
            r.mid = Nanos::ZERO;
            r
        })
    });
    assert_rejects(&e, "C08", ReasonCode::ReasonStaleReferencePrice);
}

#[test]
fn c09_collar_boundary_is_exact_to_the_nano() {
    // mid 150, 150 bps => allowed 2.25 exactly = 2_250_000_000 nanos
    let allowed = 2_250_000_000;
    for (delta, ok) in [
        (allowed, true),
        (allowed + 1, false),
        (-allowed, true),
        (-allowed - 1, false),
    ] {
        let e = with(|f| f.sig.limit_price = Some(Nanos::new(150 * SHARE + delta)));
        assert_eq!(result(&e, "C09").passed, ok, "delta {delta}");
        if !ok {
            assert_rejects(&e, "C09", ReasonCode::ReasonPriceCollar);
            assert_eq!(e.order_price, None);
        }
    }
}

#[test]
fn c09_market_orders_become_bounded_marketable_limits() {
    let e = with(|f| f.sig.limit_price = None);
    assert_eq!(e.decision, DecisionStatus::DecisionApproved);
    assert_eq!(e.order_price, Some(Nanos::new(150 * SHARE + 2_250_000_000)));
    let e = with(|f| {
        f.sig.limit_price = None;
        f.sig.side = Side::Sell;
        long_position(f, 50);
    });
    assert_eq!(e.order_price, Some(Nanos::new(150 * SHARE - 2_250_000_000)));
    let e = with(|f| {
        f.sig.limit_price = None;
        f.ref_price = None;
    });
    assert_eq!(e.decision, DecisionStatus::DecisionRejected);
    assert!(!result(&e, "C09").passed);
}

// ---- C10 / C11 / C12 ----
#[test]
fn c10_max_order_quantity_boundary() {
    let cap = 100 * SHARE;
    let e = with(|f| f.sig.qty = Nanos::new(cap));
    assert!(result(&e, "C10").passed);
    let e = with(|f| f.sig.qty = Nanos::new(cap + 1));
    assert_rejects(&e, "C10", ReasonCode::ReasonMaxOrderQuantity);
}

#[test]
fn c11_max_notional_boundary_uses_round_up() {
    // price 1.0, mid 1.0: notional == qty; cap 50_000 units = 50_000e9 nanos
    let cap = 50_000_000_000_000_i64;
    let setup = |f: &mut Fixture, qty: i64| {
        f.limits
            .symbols
            .get_mut("AAPL")
            .unwrap()
            .max_order_qty_nanos = i64::MAX;
        f.limits.symbols.get_mut("AAPL").unwrap().max_position_nanos = i64::MAX;
        f.limits.max_order_adv_bps = 10_000;
        f.limits.max_gross_exposure_nanos = i64::MAX;
        f.sig.limit_price = Some(Nanos::new(SHARE));
        f.ref_price = f.ref_price.map(|mut r| {
            r.mid = Nanos::new(SHARE);
            r.adv = Nanos::new(i64::MAX);
            r
        });
        f.sig.qty = Nanos::new(qty);
    };
    let e = with(|f| setup(f, cap));
    assert!(result(&e, "C11").passed, "{:?}", result(&e, "C11"));
    let e = with(|f| setup(f, cap + 1));
    assert_rejects(&e, "C11", ReasonCode::ReasonMaxOrderNotional);
    // sub-nano remainder rounds UP: 1 nano * (cap+1 nanos of price) is fine, but
    // a product of 50_000e9 + 0.5 nano must round to +1 and fail
    let e = with(|f| {
        setup(f, 1);
        f.limits
            .symbols
            .get_mut("AAPL")
            .unwrap()
            .max_order_notional_nanos = 1;
        f.sig.limit_price = Some(Nanos::new(SHARE + 1));
        f.ref_price = f.ref_price.map(|mut r| {
            r.mid = Nanos::new(SHARE + 1);
            r
        });
    });
    assert_rejects(&e, "C11", ReasonCode::ReasonMaxOrderNotional);
}

#[test]
fn c12_order_size_vs_adv_boundary() {
    // adv 1_000_000 sh, 50 bps => 5_000 sh
    let lift = |f: &mut Fixture| {
        f.limits
            .symbols
            .get_mut("AAPL")
            .unwrap()
            .max_order_qty_nanos = i64::MAX;
        f.limits
            .symbols
            .get_mut("AAPL")
            .unwrap()
            .max_order_notional_nanos = i64::MAX;
        f.limits.symbols.get_mut("AAPL").unwrap().max_position_nanos = i64::MAX;
        f.limits.max_gross_exposure_nanos = i64::MAX;
    };
    let e = with(|f| {
        lift(f);
        f.sig.qty = Nanos::new(5_000 * SHARE);
    });
    assert!(result(&e, "C12").passed);
    let e = with(|f| {
        lift(f);
        f.sig.qty = Nanos::new(5_000 * SHARE + 1);
    });
    assert_rejects(&e, "C12", ReasonCode::ReasonOrderSizeAdv);
    let e = with(|f| {
        f.ref_price = f.ref_price.map(|mut r| {
            r.adv = Nanos::ZERO;
            r
        })
    });
    assert_rejects(&e, "C12", ReasonCode::ReasonStateUnavailable);
}

// ---- C13 / C14 ----
#[test]
fn c13_position_limit_boundary_long_and_short() {
    // cap 500 shares
    let e = with(|f| {
        long_position(f, 400);
        f.sig.qty = Nanos::new(100 * SHARE);
    });
    assert!(result(&e, "C13").passed);
    let e = with(|f| {
        long_position(f, 400);
        f.sig.qty = Nanos::new(100 * SHARE + 1);
        f.limits
            .symbols
            .get_mut("AAPL")
            .unwrap()
            .max_order_qty_nanos = i64::MAX;
    });
    assert_rejects(&e, "C13", ReasonCode::ReasonPositionLimitSymbol);
    let e = with(|f| {
        long_position(f, -400);
        f.sig.side = Side::SellShort;
        f.limits.short_selling_enabled = true;
        f.sig.qty = Nanos::new(100 * SHARE + 1);
        f.limits
            .symbols
            .get_mut("AAPL")
            .unwrap()
            .max_order_qty_nanos = i64::MAX;
    });
    assert_rejects(&e, "C13", ReasonCode::ReasonPositionLimitSymbol);
}

#[test]
fn c13_pending_orders_count_against_the_limit() {
    let e = with(|f| {
        f.exposure = Some(ExposureView {
            position: Nanos::new(400 * SHARE),
            pending_buy: i128::from(95 * SHARE),
            pending_sell: 0,
            others_gross: Some(0),
        });
    });
    assert_rejects(&e, "C13", ReasonCode::ReasonPositionLimitSymbol);
}

#[test]
fn c13_unavailable_position_and_overflow_fail_closed() {
    let e = with(|f| f.exposure = None);
    assert_rejects(&e, "C13", ReasonCode::ReasonStateUnavailable);
    let e = with(|f| {
        f.exposure = Some(ExposureView {
            position: Nanos::new(i64::MAX),
            pending_buy: i128::MAX,
            pending_sell: 0,
            others_gross: Some(0),
        });
    });
    assert_rejects(&e, "C13", ReasonCode::ReasonInternalError);
}

#[test]
fn c14_gross_exposure_boundary_and_unavailable() {
    // own worst = 10 sh * 150 = 1_500 units. cap = 1e6 units
    let cap = 1_000_000 * SHARE;
    let own = 1_500 * SHARE;
    let e = with(|f| {
        f.exposure = f.exposure.map(|mut x| {
            x.others_gross = Some(i128::from(cap - own));
            x
        })
    });
    assert!(result(&e, "C14").passed);
    let e = with(|f| {
        f.exposure = f.exposure.map(|mut x| {
            x.others_gross = Some(i128::from(cap - own) + 1);
            x
        })
    });
    assert_rejects(&e, "C14", ReasonCode::ReasonGrossExposureLimit);
    let e = with(|f| {
        f.exposure = f.exposure.map(|mut x| {
            x.others_gross = None;
            x
        })
    });
    assert_rejects(&e, "C14", ReasonCode::ReasonStateUnavailable);
    let e = with(|f| {
        f.exposure = f.exposure.map(|mut x| {
            x.others_gross = Some(i128::MAX);
            x
        })
    });
    assert_rejects(&e, "C14", ReasonCode::ReasonInternalError);
}

// ---- C15 / C16 / C17 ----
#[test]
fn c15_drawdown_boundary() {
    // start 1e6 units, 200 bps => limit 20_000 units
    let start = 1_000_000 * SHARE;
    let limit = 20_000 * SHARE;
    let mk = |loss: i64| {
        move |f: &mut Fixture| {
            f.equity = Some(EquityView {
                day_start: Nanos::new(start),
                current: Nanos::new(start - loss),
            })
        }
    };
    let e = with(mk(limit - 1));
    assert!(result(&e, "C15").passed);
    let e = with(mk(limit));
    assert_rejects(&e, "C15", ReasonCode::ReasonDailyDrawdown);
    let e = with(mk(-500 * SHARE)); // gain
    assert!(result(&e, "C15").passed);
}

#[test]
fn c15_unavailable_or_nonpositive_start_equity_fails_closed() {
    let e = with(|f| f.equity = None);
    assert_rejects(&e, "C15", ReasonCode::ReasonStateUnavailable);
    let e = with(|f| {
        f.equity = Some(EquityView {
            day_start: Nanos::ZERO,
            current: Nanos::ZERO,
        })
    });
    assert_rejects(&e, "C15", ReasonCode::ReasonStateUnavailable);
}

#[test]
fn c16_rate_limit_global_or_symbol() {
    let e = with(|f| f.rate.global_ok = false);
    assert_rejects(&e, "C16", ReasonCode::ReasonRateLimit);
    let e = with(|f| f.rate.symbol_ok = false);
    assert_rejects(&e, "C16", ReasonCode::ReasonRateLimit);
}

#[test]
fn c17_low_omega_is_a_hard_abstain_not_a_hold() {
    let e = with(|f| f.sig.omega = 0.55);
    assert!(result(&e, "C17").passed);
    let e = with(|f| f.sig.omega = 0.549_999);
    assert_rejects(&e, "C17", ReasonCode::ReasonLowOmega);
    assert_eq!(e.signal_status, SignalStatus::SignalAbstain);
}

#[test]
fn c17_abstain_status_only_when_no_other_hard_control_failed() {
    let e = with(|f| {
        f.sig.omega = 0.1;
        f.rate.global_ok = false;
    });
    assert_eq!(e.decision, DecisionStatus::DecisionRejected);
    assert_eq!(e.signal_status, SignalStatus::SignalRejectedHardBlock);
    assert!(
        e.reasons.contains(&ReasonCode::ReasonLowOmega)
            && e.reasons.contains(&ReasonCode::ReasonRateLimit)
    );
}

// ---- C18 / C19 ----
fn regime(f: &mut Fixture, label: RegimeLabel, conf: f64) {
    f.regime = Some(RegimeView {
        label,
        confidence: conf,
        at_ns: NOW_NS - 1_000_000_000,
    });
}

#[test]
fn c18_regime_truth_table() {
    use RegimeLabel::*;
    // (signal regime, own label, own conf) -> expected reasons
    let cases = [
        (TrendingBull, TrendingBull, 0.9, vec![]),
        (TrendingBull, TrendingBull, 0.6, vec![]),
        (
            TrendingBull,
            TrendingBull,
            0.599,
            vec![ReasonCode::ReasonRegimeLowConfidence],
        ),
        (
            TrendingBull,
            LowVolChop,
            0.9,
            vec![ReasonCode::ReasonRegimeMismatch],
        ),
        (LowVolChop, LowVolChop, 0.9, vec![]),
        (
            TrendingBear,
            TrendingBear,
            0.9,
            vec![ReasonCode::ReasonRegimeMismatch],
        ), // not in validated set
        (
            TrendingBull,
            TrendingBear,
            0.5,
            vec![
                ReasonCode::ReasonRegimeLowConfidence,
                ReasonCode::ReasonRegimeMismatch,
            ],
        ),
        (
            RegimeUnknown,
            TrendingBull,
            0.9,
            vec![ReasonCode::ReasonRegimeMismatch],
        ),
        (
            TrendingBull,
            RegimeUnknown,
            0.9,
            vec![ReasonCode::ReasonRegimeMismatch],
        ),
        (
            TrendingBull,
            TrendingBull,
            f64::NAN,
            vec![ReasonCode::ReasonRegimeLowConfidence],
        ),
    ];
    for (sig_regime, own, conf, expected) in cases {
        let e = with(|f| {
            f.sig.regime = sig_regime;
            regime(f, own, conf);
        });
        let got: Vec<ReasonCode> = e
            .results
            .iter()
            .filter(|r| r.control_id == "C18" && !r.passed)
            .map(|r| ReasonCode::try_from(r.reason).unwrap())
            .collect();
        assert_eq!(got, expected, "{sig_regime:?}/{own:?}/{conf}");
        let want = if expected.is_empty() {
            DecisionStatus::DecisionApproved
        } else {
            DecisionStatus::DecisionHeldForHuman
        };
        assert_eq!(e.decision, want, "{sig_regime:?}/{own:?}/{conf}");
    }
}

#[test]
fn c18_missing_or_stale_own_regime_holds() {
    let e = with(|f| f.regime = None);
    assert_eq!(e.decision, DecisionStatus::DecisionHeldForHuman);
    let e = with(|f| {
        f.regime = Some(RegimeView {
            label: RegimeLabel::TrendingBull,
            confidence: 0.9,
            at_ns: NOW_NS - 60_000_000_001,
        })
    });
    assert_eq!(e.decision, DecisionStatus::DecisionHeldForHuman);
}

#[test]
fn c19_cold_start_and_p95_boundary() {
    let e = with(|f| {
        f.history = SizeHistory {
            count: 29,
            p95: Some(Nanos::new(20 * SHARE)),
        }
    });
    assert_eq!(e.decision, DecisionStatus::DecisionHeldForHuman);
    assert!(e.reasons.contains(&ReasonCode::ReasonUnusualOrderSize));
    let e = with(|f| f.sig.qty = Nanos::new(20 * SHARE));
    assert!(result(&e, "C19").passed);
    let e = with(|f| f.sig.qty = Nanos::new(20 * SHARE + 1));
    assert_eq!(e.decision, DecisionStatus::DecisionHeldForHuman);
    let e = with(|f| {
        f.history = SizeHistory {
            count: 100,
            p95: None,
        }
    });
    assert_eq!(e.decision, DecisionStatus::DecisionHeldForHuman);
}

#[test]
fn soft_controls_are_skipped_on_release_and_hard_still_apply() {
    let e = with(|f| {
        f.mode = Mode::Release;
        f.history = SizeHistory {
            count: 0,
            p95: None,
        };
        f.regime = None;
        f.replay = ReplayVerdict::Duplicate;
    });
    assert_eq!(e.decision, DecisionStatus::DecisionApproved);
    assert!(e
        .results
        .iter()
        .all(|r| r.control_id != "C18" && r.control_id != "C19"));
    let e = with(|f| {
        f.mode = Mode::Release;
        f.rate.global_ok = false;
    });
    assert_eq!(e.decision, DecisionStatus::DecisionRejected);
}

#[test]
fn hard_failure_wins_over_soft_and_all_phase_two_controls_are_reported() {
    let e = with(|f| {
        f.sig.qty = Nanos::new(1_000 * SHARE); // C10 (and others) fail
        f.history = SizeHistory {
            count: 0,
            p95: None,
        }; // C19 soft
        f.sig.omega = 0.1; // C17
    });
    assert_eq!(e.decision, DecisionStatus::DecisionRejected);
    for id in ["C10", "C17", "C19"] {
        assert!(!result(&e, id).passed, "{id}");
    }
    assert_eq!(e.results.len(), 18);
}

// ---- properties ----
fn arb_fixture() -> impl Strategy<Value = Fixture> {
    (
        (1i64..=i64::MAX, 1i64..=i64::MAX, 0u8..3),
        (
            -1_000 * SHARE..1_000 * SHARE,
            0i128..(1_000 * i128::from(SHARE)),
            0i128..(1_000 * i128::from(SHARE)),
        ),
        (
            0i64..=i64::MAX,
            proptest::option::of(0i32..=5),
            any::<bool>(),
        ),
        (0.0f64..=1.0, 0.0f64..=1.0),
        (-5_000_000_000_i64..5_000_000_000, 0..3usize),
    )
        .prop_map(
            |(
                (qty, price, side),
                (pos, pb, ps),
                (mid, kill, market),
                (omega, conf),
                (age_ns, rp),
            )| {
                let mut f = Fixture::new();
                f.sig.qty = Nanos::new(qty);
                f.sig.limit_price = if market {
                    None
                } else {
                    Some(Nanos::new(price))
                };
                f.sig.side = [Side::Buy, Side::Sell, Side::SellShort][usize::from(side)];
                f.sig.omega = omega;
                f.sig.created_at_ns = NOW_NS - age_ns;
                f.limits.short_selling_enabled = rp == 0;
                f.kill = kill.and_then(KillSwitchLevel::from_wire);
                f.ref_price = f.ref_price.map(|mut r| {
                    r.mid = Nanos::new(mid);
                    r
                });
                f.exposure = Some(ExposureView {
                    position: Nanos::new(pos),
                    pending_buy: pb,
                    pending_sell: ps,
                    others_gross: Some(0),
                });
                f.regime = f.regime.map(|mut r| {
                    r.confidence = conf;
                    r
                });
                f
            },
        )
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2_000))]

    /// APPROVED implies every control passed, a bounded price exists, and the
    /// independently recomputed limits hold.
    #[test]
    fn approved_implies_every_hard_control_passed(f in arb_fixture()) {
        let e = f.run();
        if e.decision == DecisionStatus::DecisionApproved {
            prop_assert!(e.results.iter().all(|r| r.passed));
            prop_assert!(e.reasons.is_empty());
            let price = e.order_price.expect("approved order has a price").get();
            prop_assert!(price > 0);
            prop_assert!(matches!(
                f.kill,
                Some(KillSwitchLevel::KillLevelNormal | KillSwitchLevel::KillLevelSoft)
            ));
            let lim = &f.limits.symbols["AAPL"];
            prop_assert!(f.sig.qty.get() <= lim.max_order_qty_nanos);
            let notional = (i128::from(price) * i128::from(f.sig.qty.get()) + 999_999_999) / 1_000_000_000;
            prop_assert!(notional <= i128::from(lim.max_order_notional_nanos));
            let mid = i128::from(f.ref_price.unwrap().mid.get());
            prop_assert!((i128::from(price) - mid).abs() <= mid * 150 / 10_000);
            prop_assert!(f.sig.omega >= f.limits.omega_min);
        }
    }

    /// No kill state / DEAD_MANS / HARD / PHYSICAL never approves; the
    /// pipeline never panics on extreme inputs.
    #[test]
    fn severe_or_unknown_kill_state_never_approves(f in arb_fixture(), lvl in 3i32..=5) {
        let mut f = f;
        f.kill = KillSwitchLevel::from_wire(lvl);
        prop_assert_ne!(f.run().decision, DecisionStatus::DecisionApproved);
        f.kill = KillSwitchLevel::from_wire(99);
        prop_assert_ne!(f.run().decision, DecisionStatus::DecisionApproved);
    }

    /// Extreme values (overflow territory) never approve and never panic.
    #[test]
    fn extreme_values_are_rejected_not_panicked(qty in prop_oneof![Just(i64::MAX), Just(1i64), 1i64..i64::MAX], price in prop_oneof![Just(i64::MAX), Just(1i64)], pos in prop_oneof![Just(i64::MIN), Just(i64::MAX), Just(0i64)]) {
        let mut f = Fixture::new();
        f.sig.qty = Nanos::new(qty);
        f.sig.limit_price = Some(Nanos::new(price));
        f.exposure = Some(ExposureView { position: Nanos::new(pos), pending_buy: i128::MAX, pending_sell: i128::MAX, others_gross: Some(i128::MAX) });
        f.ref_price = f.ref_price.map(|mut r| { r.mid = Nanos::new(price); r });
        let e = f.run();
        prop_assert_ne!(e.decision, DecisionStatus::DecisionApproved);
    }
}
