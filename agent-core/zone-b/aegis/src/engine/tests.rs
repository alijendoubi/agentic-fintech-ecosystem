//! Engine tests: the end-to-end decision path with every dependency in memory.

use std::sync::Arc;

use prost::Message;

use crate::audit::{AuditEvent, FailingSink};
use crate::hex;
use crate::killswitch::store::MemoryKillStore;
use crate::money::Nanos;
use crate::pb::{
    self, DecisionStatus as D, KillSwitchLevel as L, ReasonCode as R, SignalStatus as S,
};
use crate::signing::verify::{verify_attestation, ShortPolicy};
use crate::signing::{Algorithm, SignError, Signer};
use crate::state::replay::UnavailableReplayStore;
use crate::testkit::{uuid_n, Rig, RigOptions, NOW_NS, SHARE};

fn status(d: &pb::AegisDecision) -> D {
    D::try_from(d.decision).unwrap()
}

fn has_reason(d: &pb::AegisDecision, r: R) -> bool {
    d.reasons.contains(&(r as i32))
}

fn approved(rig: &Rig, n: u64, shares: i64) -> pb::AegisDecision {
    let d = rig.engine.submit_signal(&rig.signal(n, shares));
    assert_eq!(status(&d), D::DecisionApproved, "{:?}", d.reasons);
    d
}

// ---- approval ----
#[test]
fn baseline_signal_is_approved_and_the_attestation_verifies_against_the_order() {
    let rig = Rig::default();
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionApproved);
    assert_eq!(d.signal_status, S::SignalApproved as i32);
    let (order, att) = (d.order.clone().unwrap(), d.attestation.clone().unwrap());
    assert_eq!(order.quantity_nanos, 10 * SHARE);
    assert_eq!(order.limit_price_nanos, 150 * SHARE);
    assert_eq!(
        order.order_id, order.signal_id,
        "client order id is bound through the signal id"
    );
    assert_eq!(att.limits_config_sha256, rig.limits.sha256_hex);
    assert_eq!(att.expires_at_ns, NOW_NS + 5_000_000_000);
    let side = verify_attestation(
        &order,
        &att,
        NOW_NS + 1,
        &rig.verifier(),
        ShortPolicy::Refuse,
    );
    assert_eq!(side, Ok(crate::domain::Side::Buy));
    assert!(d.hold_id.is_empty());
    assert!(!d.results.is_empty() && d.results.iter().all(|r| r.passed));
}

#[test]
fn approval_reserves_exposure_persists_it_and_is_audited_without_llm_text() {
    let rig = Rig::default();
    approved(&rig, 1, 10);
    let saved = rig.portfolio_store.saved().unwrap();
    assert_eq!(saved.open_orders.len(), 1);
    assert_eq!(saved.open_orders[&uuid_n(1)].qty, 10 * SHARE);
    let events = rig.audit.events();
    let AuditEvent::Decision(rec) = events
        .iter()
        .rev()
        .find(|e| matches!(e, AuditEvent::Decision(_)))
        .unwrap()
    else {
        unreachable!()
    };
    assert_eq!(rec.decision, "DECISION_APPROVED");
    assert_eq!(rec.attestation_key_id, rig.signer.key_id());
    assert!(!rec.controls.is_empty());
    let text = serde_json::to_string(&events).unwrap();
    assert!(!text.contains("debate"), "no LLM text in audit");
}

#[test]
fn market_orders_are_attested_as_bounded_marketable_limits() {
    let rig = Rig::default();
    let mut s = rig.signal(1, 10);
    s.price_limit_nanos = 0;
    let d = rig.engine.submit_signal(&s);
    assert_eq!(status(&d), D::DecisionApproved);
    assert_eq!(
        d.order.unwrap().limit_price_nanos,
        150 * SHARE + 2_250_000_000
    );
}

// ---- idempotency and replay ----
#[test]
fn duplicate_signal_returns_the_original_decision_without_a_second_attestation() {
    let rig = Rig::default();
    let s = rig.signal(1, 10);
    let first = rig.engine.submit_signal(&s);
    let second = rig.engine.submit_signal(&s);
    assert_eq!(first, second, "byte-identical original decision");
    assert_eq!(
        rig.portfolio_store.saved().unwrap().open_orders.len(),
        1,
        "no second reservation"
    );
    let decisions = rig
        .audit
        .events()
        .iter()
        .filter(|e| matches!(e, AuditEvent::Decision(_)))
        .count();
    assert_eq!(decisions, 1, "the duplicate is not a new decision");
}

#[test]
fn same_id_with_a_different_payload_is_a_replay_mismatch_and_a_security_event() {
    let rig = Rig::default();
    let first = rig.engine.submit_signal(&rig.signal(1, 10));
    let mut tampered = rig.signal(1, 11);
    tampered.signal_id = uuid_n(1);
    let d = rig.engine.submit_signal(&tampered);
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonReplayPayloadMismatch));
    assert!(d.attestation.is_none());
    assert!(rig.audit.events().iter().any(
        |e| matches!(e, AuditEvent::Security { kind, .. } if kind == "replay_payload_mismatch")
    ));
    // the original record is untouched
    assert_eq!(rig.engine.submit_signal(&rig.signal(1, 10)), first);
}

#[test]
fn replay_store_failure_rejects() {
    let rig = Rig::build(RigOptions {
        replay: Some(Arc::new(UnavailableReplayStore)),
        ..RigOptions::default()
    });
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonStateUnavailable));
    assert!(d.attestation.is_none() && d.order.is_none());
}

// ---- validation / expiry ----
#[test]
fn invalid_payloads_are_rejected_and_audited() {
    let rig = Rig::default();
    let mut s = rig.signal(1, 10);
    s.side = 77;
    let d = rig.engine.submit_signal(&s);
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonInvalidSignal));
    assert_eq!(d.results[0].control_id, "C02");
    assert!(rig.audit.events().iter().any(|e| matches!(e, AuditEvent::Decision(r) if r.reasons.contains(&"REASON_INVALID_SIGNAL".to_owned()))));
}

#[test]
fn expired_and_future_signals_are_rejected() {
    let rig = Rig::default();
    let mut s = rig.signal(1, 10);
    s.valid_until_ns = NOW_NS - 1;
    let d = rig.engine.submit_signal(&s);
    assert!(has_reason(&d, R::ReasonSignalExpired));
    assert_eq!(d.signal_status, S::SignalExpired as i32);
    let mut s = rig.signal(2, 10);
    s.created_at_ns = NOW_NS + 10_000_000_000;
    assert!(has_reason(
        &rig.engine.submit_signal(&s),
        R::ReasonSignalFromFuture
    ));
}

#[test]
fn stale_market_data_rejects_and_no_feed_means_no_trades() {
    let rig = Rig::default();
    rig.clock.advance_ms(2_000);
    let mut s = rig.signal(1, 10);
    s.created_at_ns = rig.now_ns() - 100_000_000;
    s.valid_until_ns = rig.now_ns() + 4_000_000_000;
    let d = rig.engine.submit_signal(&s);
    assert!(has_reason(&d, R::ReasonStaleReferencePrice));
    assert_eq!(status(&d), D::DecisionRejected);
}

// ---- kill switch (spec 5.4 invariants 5 and 6) ----
#[test]
fn no_attestation_is_produced_at_logic_dead_mans_hard_or_physical_for_risk_increasing_orders() {
    for lvl in [
        L::KillLevelSoft,
        L::KillLevelLogic,
        L::KillLevelDeadMans,
        L::KillLevelHard,
        L::KillLevelPhysical,
    ] {
        let rig = Rig::default();
        rig.kill.trigger(lvl as i32, "test", "tester").unwrap();
        let d = rig.engine.submit_signal(&rig.signal(1, 10));
        assert_ne!(status(&d), D::DecisionApproved, "{lvl:?}");
        assert!(d.attestation.is_none() && d.order.is_none(), "{lvl:?}");
        assert!(has_reason(&d, R::ReasonKillSwitchActive), "{lvl:?}");
    }
}

#[test]
fn unreadable_kill_state_at_startup_means_hard_and_no_trades() {
    let store = Arc::new(MemoryKillStore::default());
    store.set_unreadable(true);
    let rig = Rig::build(RigOptions {
        kill_store: Some(store),
        ..RigOptions::default()
    });
    assert_eq!(rig.kill.effective_level(), Some(L::KillLevelHard));
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(d.attestation.is_none());
}

#[test]
fn a_reducing_order_is_held_at_logic_and_approved_only_after_human_release() {
    let rig = Rig::default();
    let mut p = rig.portfolio_store.saved().unwrap();
    p.positions.insert("AAPL".into(), 50 * SHARE);
    crate::state::portfolio::PortfolioStore::save(&*rig.portfolio_store, &p).unwrap();
    let rig = Rig::build(RigOptions {
        portfolio_store: Some(rig.portfolio_store.clone()),
        ..RigOptions::default()
    });
    rig.kill
        .trigger(L::KillLevelLogic as i32, "drawdown", "aegis/drawdown")
        .unwrap();
    let mut s = rig.signal(1, 10);
    s.side = pb::SignalSide::Sell as i32;
    let d = rig.engine.submit_signal(&s);
    assert_eq!(status(&d), D::DecisionHeldForHuman, "{:?}", d.reasons);
    assert!(d.attestation.is_none());
    let released = rig.engine.resolve_hold(&pb::ResolveHoldRequest {
        hold_id: d.hold_id.clone(),
        operator_id: "op-1".into(),
        approve: true,
        ..pb::ResolveHoldRequest::default()
    });
    let released = released.unwrap();
    assert_eq!(
        status(&released),
        D::DecisionApproved,
        "{:?}",
        released.reasons
    );
    assert!(released.attestation.is_some());
}

// ---- fail-closed dependencies ----
struct DownSigner;
impl Signer for DownSigner {
    fn key_id(&self) -> &str {
        "down-key"
    }
    fn algorithm(&self) -> Algorithm {
        Algorithm::EcdsaP256Sha256
    }
    fn sign(&self, _d: &[u8; 32]) -> Result<Vec<u8>, SignError> {
        Err(SignError::Unavailable("hsm down".into()))
    }
    fn is_healthy(&self) -> bool {
        false
    }
}

#[test]
fn hsm_failure_rejects_without_reserving_exposure() {
    let rig = Rig::build(RigOptions {
        signer: Some(Arc::new(DownSigner)),
        ..RigOptions::default()
    });
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonHsmUnavailable));
    assert!(d.attestation.is_none() && d.order.is_none());
    assert!(rig.portfolio_store.saved().unwrap().open_orders.is_empty());
    assert!(!rig.engine.aegis_state().hsm_ok);
}

#[test]
fn mandatory_audit_failure_rejects_and_the_replay_returns_the_rejection() {
    let rig = Rig::build(RigOptions {
        audit: Some(Arc::new(FailingSink)),
        ..RigOptions::default()
    });
    let s = rig.signal(1, 10);
    let d = rig.engine.submit_signal(&s);
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonAuditUnavailable));
    assert!(d.attestation.is_none() && d.order.is_none());
    assert_eq!(
        rig.engine.submit_signal(&s),
        d,
        "the approval is never resurrected by a retry"
    );
    assert!(!rig.engine.aegis_state().audit_sink_ok);
}

#[test]
fn optional_audit_failure_does_not_block_trading() {
    let json = crate::testkit::limits_json()
        .replace("\"audit_mandatory\": true", "\"audit_mandatory\": false");
    let rig = Rig::build(RigOptions {
        limits_json: Some(json),
        audit: Some(Arc::new(FailingSink)),
        ..RigOptions::default()
    });
    assert_eq!(
        status(&rig.engine.submit_signal(&rig.signal(1, 10))),
        D::DecisionApproved
    );
}

#[test]
fn portfolio_persist_failure_rejects_approval() {
    let rig = Rig::default();
    rig.portfolio_store.set_fail_saves(true);
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonStateUnavailable));
    assert!(d.attestation.is_none());
}

#[test]
fn unreadable_portfolio_state_rejects_everything() {
    let store = Arc::new(crate::state::portfolio::MemoryPortfolioStore::default());
    struct Broken;
    impl crate::state::portfolio::PortfolioStore for Broken {
        fn load(&self) -> Result<crate::state::portfolio::Portfolio, crate::state::StateError> {
            Err(crate::state::StateError::Unavailable("corrupt".into()))
        }
        fn save(
            &self,
            _p: &crate::state::portfolio::Portfolio,
        ) -> Result<(), crate::state::StateError> {
            Err(crate::state::StateError::Unavailable("corrupt".into()))
        }
    }
    let _ = store;
    let rig = Rig::default();
    let engine = crate::engine::Engine::new(crate::engine::EngineDeps {
        portfolio_store: Arc::new(Broken),
        limits: rig.limits.clone(),
        clock: Arc::new(rig.clock.clone()),
        kill: rig.kill.clone(),
        signer: rig.signer.clone(),
        audit: rig.audit.clone(),
        replay: Arc::new(crate::state::replay::MemoryReplayStore::new(86_400_000)),
        refdata: rig.refdata.clone(),
    });
    let d = engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(d.attestation.is_none());
}

// ---- exposure, drawdown, rate ----
#[test]
fn pending_approvals_count_against_position_limits_until_released() {
    let rig = Rig::default();
    let mut ids = Vec::new();
    for n in 1..=5u64 {
        ids.push(approved(&rig, n, 100).order.unwrap().order_id);
        rig.clock.advance_ms(1_000);
        rig.refresh_market();
    }
    let d = rig.engine.submit_signal(&rig.signal(6, 100));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(
        has_reason(&d, R::ReasonPositionLimitSymbol),
        "{:?}",
        d.reasons
    );
    // the motor reports the first order cancelled: 100 shares of headroom return
    let ack = rig.engine.report_execution(&pb::ExecutionReport {
        order_id: ids[0].clone(),
        signal_id: ids[0].clone(),
        symbol: "AAPL".into(),
        side: pb::OrderSide::OrderBuy as i32,
        status: pb::OrderStatus::OrderCancelled as i32,
        ..pb::ExecutionReport::default()
    });
    assert!(ack.ok, "{}", ack.detail);
    rig.clock.advance_ms(1_000);
    rig.refresh_market();
    assert_eq!(
        status(&rig.engine.submit_signal(&rig.signal(7, 100))),
        D::DecisionApproved
    );
}

#[test]
fn fills_move_the_position_idempotently() {
    let rig = Rig::default();
    let id = approved(&rig, 1, 10).order.unwrap().order_id;
    let report = pb::ExecutionReport {
        order_id: id.clone(),
        signal_id: id,
        symbol: "AAPL".into(),
        side: pb::OrderSide::OrderBuy as i32,
        status: pb::OrderStatus::OrderFilled as i32,
        filled_qty_nanos: 10 * SHARE,
        ..pb::ExecutionReport::default()
    };
    assert!(rig.engine.report_execution(&report).ok);
    assert!(rig.engine.report_execution(&report).ok);
    assert_eq!(
        rig.portfolio_store.saved().unwrap().positions["AAPL"],
        10 * SHARE
    );
}

#[test]
fn execution_reports_are_validated() {
    let rig = Rig::default();
    let mk = |f: fn(&mut pb::ExecutionReport)| {
        let mut r = pb::ExecutionReport {
            order_id: uuid_n(9),
            symbol: "AAPL".into(),
            side: pb::OrderSide::OrderBuy as i32,
            status: pb::OrderStatus::OrderPartial as i32,
            filled_qty_nanos: SHARE,
            ..pb::ExecutionReport::default()
        };
        f(&mut r);
        rig.engine.report_execution(&r)
    };
    assert!(mk(|_| {}).ok);
    assert!(!mk(|r| r.filled_qty_nanos = -1).ok);
    assert!(!mk(|r| r.account_equity_nanos = -5).ok);
    assert!(!mk(|r| r.status = 99).ok);
    assert!(!mk(|r| r.side = 0).ok);
    assert!(
        !mk(|r| r.filled_qty_nanos = 0).ok,
        "cumulative fill may not decrease"
    );
}

#[test]
fn drawdown_breach_latches_logic_and_blocks_new_risk() {
    let rig = Rig::default();
    let ack = rig.engine.report_execution(&pb::ExecutionReport {
        account_equity_nanos: 979_000 * SHARE, // 2.1% below the 1,000,000 baseline
        ..pb::ExecutionReport::default()
    });
    assert!(ack.ok);
    assert_eq!(rig.kill.effective_level(), Some(L::KillLevelLogic));
    assert!(rig
        .kill
        .state()
        .latches
        .iter()
        .any(|l| l.actor_id == "aegis/drawdown"));
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonKillSwitchActive));
    // a second breaching report does not stack another latch
    rig.engine.report_execution(&pb::ExecutionReport {
        account_equity_nanos: 970_000 * SHARE,
        ..pb::ExecutionReport::default()
    });
    assert_eq!(rig.kill.state().latches.len(), 1);
}

#[test]
fn without_a_day_start_equity_nothing_is_approved() {
    let rig = Rig::build(RigOptions {
        no_equity: true,
        ..RigOptions::default()
    });
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonStateUnavailable));
}

#[test]
fn per_symbol_rate_limit_rejects_the_sixth_signal_in_a_burst() {
    let rig = Rig::default();
    for n in 1..=5 {
        assert_eq!(
            status(&rig.engine.submit_signal(&rig.signal(n, 1))),
            D::DecisionApproved,
            "{n}"
        );
    }
    let d = rig.engine.submit_signal(&rig.signal(6, 1));
    assert!(has_reason(&d, R::ReasonRateLimit));
}

// ---- holds ----
fn held(rig: &Rig, n: u64) -> pb::AegisDecision {
    let d = rig.engine.submit_signal(&rig.signal(n, 10));
    assert_eq!(status(&d), D::DecisionHeldForHuman, "{:?}", d.reasons);
    assert_eq!(d.signal_status, S::SignalSoftBlockPending as i32);
    assert!(!d.hold_id.is_empty() && d.hold_expires_at_ns > NOW_NS && d.attestation.is_none());
    d
}

fn resolve(rig: &Rig, id: &str, approve: bool) -> Result<pb::AegisDecision, super::HoldError> {
    rig.engine.resolve_hold(&pb::ResolveHoldRequest {
        hold_id: id.into(),
        operator_id: "op-1".into(),
        approve,
        ..pb::ResolveHoldRequest::default()
    })
}

#[test]
fn cold_start_holds_and_a_human_release_approves_with_an_attestation() {
    let rig = Rig::build(RigOptions {
        cold_start: true,
        ..RigOptions::default()
    });
    let h = held(&rig, 1);
    assert_eq!(rig.engine.aegis_state().open_holds, 1);
    assert!(h.hold_expires_at_ns <= NOW_NS + 60_000_000_000);
    let d = resolve(&rig, &h.hold_id, true).unwrap();
    assert_eq!(status(&d), D::DecisionApproved, "{:?}", d.reasons);
    let (o, a) = (d.order.unwrap(), d.attestation.unwrap());
    assert!(verify_attestation(&o, &a, NOW_NS + 1, &rig.verifier(), ShortPolicy::Refuse).is_ok());
    assert_eq!(rig.engine.aegis_state().open_holds, 0);
    // a retry of the original submission now returns the FINAL decision
    let again = rig.engine.submit_signal(&rig.signal(1, 10));
    assert_eq!(status(&again), D::DecisionApproved);
    assert_eq!(again.attestation.unwrap().signature, a.signature);
    assert!(
        matches!(
            resolve(&rig, &h.hold_id, true),
            Err(super::HoldError::NotFound)
        ),
        "single use"
    );
}

#[test]
fn operator_rejection_expiry_and_unknown_holds() {
    let rig = Rig::build(RigOptions {
        cold_start: true,
        ..RigOptions::default()
    });
    let h = held(&rig, 1);
    let d = resolve(&rig, &h.hold_id, false).unwrap();
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(has_reason(&d, R::ReasonHoldRejectedByOperator));
    assert!(matches!(
        resolve(&rig, "nope", true),
        Err(super::HoldError::NotFound)
    ));

    rig.clock.advance_ms(1_000);
    rig.refresh_market();
    let h2 = held(&rig, 2);
    rig.clock.advance_ms(61_000);
    rig.engine.maintenance();
    assert_eq!(rig.engine.aegis_state().open_holds, 0);
    assert!(matches!(
        resolve(&rig, &h2.hold_id, true),
        Err(super::HoldError::NotFound)
    ));
    let mut s2 = rig.signal(2, 10);
    s2.created_at_ns = NOW_NS + 1_000_000_000 - 100_000_000;
    s2.valid_until_ns = NOW_NS + 1_000_000_000 + 4_000_000_000;
    let replayed = rig.engine.submit_signal(&s2);
    assert!(
        has_reason(&replayed, R::ReasonHoldExpired),
        "{:?}",
        replayed.reasons
    );
}

#[test]
fn release_reruns_hard_controls_a_human_cannot_override_them() {
    let rig = Rig::build(RigOptions {
        cold_start: true,
        ..RigOptions::default()
    });
    let h = held(&rig, 1);
    rig.kill
        .trigger(L::KillLevelHard as i32, "incident", "operator")
        .unwrap();
    let d = resolve(&rig, &h.hold_id, true).unwrap();
    assert_eq!(status(&d), D::DecisionRejected);
    assert!(d.attestation.is_none() && has_reason(&d, R::ReasonKillSwitchActive));
    // and a stale price at release time is also fatal
    let rig = Rig::build(RigOptions {
        cold_start: true,
        ..RigOptions::default()
    });
    let h = held(&rig, 1);
    rig.clock.advance_ms(3_000);
    let d = resolve(&rig, &h.hold_id, true).unwrap();
    assert_eq!(status(&d), D::DecisionRejected);
}

#[test]
fn release_requests_are_validated_and_refusals_keep_the_hold() {
    let json = crate::testkit::limits_json()
        .replace(
            "\"hold_requires_second_approver\": false",
            "\"hold_requires_second_approver\": true",
        )
        .replace(
            "\"hold_requires_cooling_period\": false",
            "\"hold_requires_cooling_period\": true",
        );
    let rig = Rig::build(RigOptions {
        cold_start: true,
        limits_json: Some(json),
        ..RigOptions::default()
    });
    let h = held(&rig, 1);
    let mk = |f: fn(&mut pb::ResolveHoldRequest)| {
        let mut r = pb::ResolveHoldRequest {
            hold_id: h.hold_id.clone(),
            operator_id: "op-1".into(),
            second_approver_id: "op-2".into(),
            approve: true,
            cooling_period_enforced: true,
            reverse_guardrail_distress_score: 0.1,
            ..pb::ResolveHoldRequest::default()
        };
        f(&mut r);
        rig.engine.resolve_hold(&r)
    };
    for bad in [
        (|r: &mut pb::ResolveHoldRequest| r.operator_id = String::new())
            as fn(&mut pb::ResolveHoldRequest),
        |r| r.second_approver_id = String::new(),
        |r| r.second_approver_id = "op-1".into(),
        |r| r.cooling_period_enforced = false,
        |r| r.reverse_guardrail_distress_score = f64::NAN,
        |r| r.reverse_guardrail_distress_score = 0.8,
        |r| r.reverse_guardrail_distress_score = -0.1,
    ] {
        assert!(matches!(mk(bad), Err(super::HoldError::Refused(_))));
    }
    assert_eq!(
        rig.engine.aegis_state().open_holds,
        1,
        "refusals keep the hold pending"
    );
    assert_eq!(status(&mk(|_| {}).unwrap()), D::DecisionApproved);
}

// ---- state and codec ----
#[test]
fn aegis_state_reports_health_hash_and_version() {
    let rig = Rig::default();
    let st = rig.engine.aegis_state();
    assert!(st.hsm_ok && st.audit_sink_ok && st.reference_data_fresh);
    assert_eq!(st.limits_config_sha256, rig.limits.sha256_hex);
    assert_eq!(st.build_version, crate::VERSION);
    assert_eq!(st.kill.unwrap().effective_level, L::KillLevelNormal as i32);
}

#[test]
fn stored_decisions_round_trip_through_the_replay_codec() {
    let rig = Rig::default();
    let d = rig.engine.submit_signal(&rig.signal(1, 10));
    let bytes = d.encode_to_vec();
    assert_eq!(pb::AegisDecision::decode(bytes.as_slice()).unwrap(), d);
    assert_eq!(hex::decode(&hex::encode(&bytes)).unwrap(), bytes);
    let _ = Nanos::ZERO;
}
