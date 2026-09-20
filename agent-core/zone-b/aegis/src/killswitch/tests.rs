//! State-machine and controller tests. Each spec 5.4 invariant has a test.

use std::sync::Arc;

use super::controller::{ControllerError, KillController};
use super::store::{KillStore, MemoryKillStore};
use super::*;
use crate::audit::{AuditEvent, FailingSink, MemorySink};
use crate::clock::ManualClock;
use crate::pb::KillSwitchLevel as L;

const HOUR_NS: i64 = 3_600_000_000_000;
const T0: i64 = 1_000 * HOUR_NS;

fn cfg() -> HeartbeatConfig {
    HeartbeatConfig::from_ms(4 * 3_600_000, 3 * 3_600_000 + 1_800_000)
}

fn approval(id: &str, role: Role) -> VerifiedApproval {
    VerifiedApproval {
        approver_id: id.into(),
        role,
    }
}

fn req(trigger: &str, approvals: Vec<VerifiedApproval>, root: &str) -> ResetRequest {
    ResetRequest {
        trigger_id: trigger.into(),
        approvals,
        root_cause_ref: root.into(),
    }
}

fn trigger_id(ev: KillEvent) -> String {
    match ev {
        KillEvent::Latched { trigger_id, .. } => trigger_id,
        other => panic!("not a latch: {other:?}"),
    }
}

// ---- invariant 1: effective level is max(active latches) ----
#[test]
fn effective_level_is_the_max_over_latches_and_reset_of_one_keeps_the_others() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    assert_eq!(ks.effective_level(), L::KillLevelNormal);
    let soft = trigger_id(ks.trigger(L::KillLevelSoft as i32, "s", "op1", T0).unwrap());
    let logic = trigger_id(
        ks.trigger(L::KillLevelLogic as i32, "l", "mon", T0)
            .unwrap(),
    );
    assert_eq!(ks.effective_level(), L::KillLevelLogic);
    ks.reset(
        &req(
            &logic,
            vec![approval("a", Role::Operator), approval("b", Role::Operator)],
            "RCA-1",
        ),
        T0,
    )
    .unwrap();
    assert_eq!(
        ks.effective_level(),
        L::KillLevelSoft,
        "other latch untouched"
    );
    ks.reset(&req(&soft, vec![approval("a", Role::Operator)], ""), T0)
        .unwrap();
    assert_eq!(ks.effective_level(), L::KillLevelNormal);
}

#[test]
fn every_level_is_reachable_and_ordered() {
    for lvl in [
        L::KillLevelSoft,
        L::KillLevelLogic,
        L::KillLevelDeadMans,
        L::KillLevelHard,
        L::KillLevelPhysical,
    ] {
        let mut ks = KillSwitch::fresh(T0, cfg());
        ks.trigger(lvl as i32, "x", "a", T0).unwrap();
        assert_eq!(ks.effective_level(), lvl);
    }
}

// ---- NORMAL is the zero value: malformed triggers must not fail open ----
#[test]
fn trigger_with_unset_or_unknown_level_latches_hard_never_normal() {
    for raw in [0, 6, 99, -1, i32::MAX, i32::MIN] {
        let mut ks = KillSwitch::fresh(T0, cfg());
        ks.trigger(raw, "monitor bug", "mon", T0).unwrap();
        assert_eq!(ks.effective_level(), L::KillLevelHard, "raw {raw}");
        assert!(ks.state().latches[0].reason.contains("malformed"));
    }
}

#[test]
fn persisted_latch_with_unknown_level_counts_as_hard() {
    let state = KillState {
        latches: vec![Latch {
            trigger_id: "t".into(),
            level: 77,
            reason: String::new(),
            actor_id: "x".into(),
            latched_at_ns: 1,
        }],
        state_seq: 2,
        updated_at_ns: 1,
        last_operator_heartbeat_ns: T0,
    };
    assert_eq!(
        KillSwitch::from_persisted(state, cfg()).effective_level(),
        L::KillLevelHard
    );
}

// ---- invariant 2: no automatic transition reduces a latch ----
#[test]
fn heartbeat_and_tick_never_lower_the_level() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    ks.trigger(L::KillLevelSoft as i32, "s", "op", T0).unwrap();
    ks.heartbeat("op", T0 + HOUR_NS);
    ks.tick(T0 + 2 * HOUR_NS);
    assert_eq!(ks.effective_level(), L::KillLevelSoft);
    // a dead-man's latch is not cleared by a heartbeat either
    ks.tick(T0 + 10 * HOUR_NS);
    assert_eq!(ks.effective_level(), L::KillLevelDeadMans);
    ks.heartbeat("op", T0 + 11 * HOUR_NS);
    assert_eq!(ks.effective_level(), L::KillLevelDeadMans);
}

#[test]
fn state_seq_increases_on_every_change() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    let mut last = ks.state().state_seq;
    let mut check = |ks: &KillSwitch| {
        assert!(ks.state().state_seq > last);
        last = ks.state().state_seq;
    };
    let id = trigger_id(ks.trigger(L::KillLevelSoft as i32, "s", "op", T0).unwrap());
    check(&ks);
    ks.heartbeat("op", T0 + 1);
    check(&ks);
    ks.reset(&req(&id, vec![approval("a", Role::Operator)], ""), T0 + 2)
        .unwrap();
    check(&ks);
}

// ---- dead-man's switch: 4 h operator heartbeat ----
#[test]
fn dead_mans_trips_after_4h_exactly_once_and_warns_at_3h30() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    assert!(ks.tick(T0 + 3 * HOUR_NS).is_empty());
    assert_eq!(
        ks.tick(T0 + 3 * HOUR_NS + HOUR_NS / 2 + 1),
        vec![KillEvent::HeartbeatWarning]
    );
    assert!(
        ks.tick(T0 + 3 * HOUR_NS + HOUR_NS / 2 + 2).is_empty(),
        "warn once"
    );
    assert!(
        ks.tick(T0 + 4 * HOUR_NS).is_empty(),
        "deadline itself is not a lapse"
    );
    assert_eq!(ks.effective_level(), L::KillLevelNormal);
    let ev = ks.tick(T0 + 4 * HOUR_NS + 1);
    assert!(
        matches!(&ev[0], KillEvent::Latched { level: L::KillLevelDeadMans, actor_id, .. } if actor_id == HEARTBEAT_ACTOR)
    );
    assert!(
        ks.tick(T0 + 5 * HOUR_NS).is_empty(),
        "no second dead-man latch"
    );
    assert_eq!(ks.state().latches.len(), 1);
}

#[test]
fn heartbeat_extends_the_deadline_and_rearms_the_warning() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    ks.tick(T0 + 3 * HOUR_NS + HOUR_NS / 2 + 1);
    ks.heartbeat("op", T0 + 3 * HOUR_NS + HOUR_NS / 2 + 2);
    assert_eq!(
        ks.heartbeat_deadline_ns(),
        T0 + 3 * HOUR_NS + HOUR_NS / 2 + 2 + 4 * HOUR_NS
    );
    assert!(ks.tick(T0 + 5 * HOUR_NS).is_empty());
    assert_eq!(ks.effective_level(), L::KillLevelNormal);
}

#[test]
fn restart_with_a_stale_persisted_heartbeat_trips_dead_mans_on_first_tick() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    ks.heartbeat("op", T0);
    let persisted = ks.state().clone();
    let mut restarted = KillSwitch::from_persisted(persisted, cfg());
    restarted.tick(T0 + 5 * HOUR_NS);
    assert_eq!(restarted.effective_level(), L::KillLevelDeadMans);
}

// ---- invariant 4: reset authority per level ----
#[test]
fn reset_requirements_per_level() {
    let op = |id: &str| approval(id, Role::Operator);
    let co = |id: &str| approval(id, Role::Compliance);
    struct Case {
        lvl: L,
        ok: Vec<VerifiedApproval>,
        root: &'static str,
        short: Vec<VerifiedApproval>,
    }
    let cases = [
        Case {
            lvl: L::KillLevelSoft,
            ok: vec![op("a")],
            root: "",
            short: vec![],
        },
        Case {
            lvl: L::KillLevelSoft,
            ok: vec![op("a")],
            root: "",
            short: vec![co("c")],
        },
        Case {
            lvl: L::KillLevelLogic,
            ok: vec![op("a"), op("b")],
            root: "RCA",
            short: vec![op("a")],
        },
        Case {
            lvl: L::KillLevelHard,
            ok: vec![op("a"), op("b"), co("c")],
            root: "RCA",
            short: vec![op("a"), op("b")],
        },
        Case {
            lvl: L::KillLevelPhysical,
            ok: vec![op("a"), op("b"), co("c")],
            root: "INC-7",
            short: vec![op("a"), co("c")],
        },
    ];
    for c in cases {
        let mut ks = KillSwitch::fresh(T0, cfg());
        let id = trigger_id(ks.trigger(c.lvl as i32, "x", "m", T0).unwrap());
        let refused = ks.reset(&req(&id, c.short.clone(), c.root), T0 + 1);
        assert!(
            matches!(refused, Err(ResetRefusal::InsufficientApprovals { .. })),
            "{:?} short {:?}",
            c.lvl,
            refused
        );
        assert_eq!(ks.effective_level(), c.lvl, "refused reset changes nothing");
        assert!(
            ks.reset(&req(&id, c.ok.clone(), c.root), T0 + 2).is_ok(),
            "{:?}",
            c.lvl
        );
        assert_eq!(ks.effective_level(), L::KillLevelNormal);
    }
}

#[test]
fn reset_with_no_approvals_is_refused_at_every_level() {
    for lvl in [
        L::KillLevelSoft,
        L::KillLevelLogic,
        L::KillLevelDeadMans,
        L::KillLevelHard,
        L::KillLevelPhysical,
    ] {
        let mut ks = KillSwitch::fresh(T0, cfg());
        let id = trigger_id(ks.trigger(lvl as i32, "x", "m", T0).unwrap());
        assert!(
            ks.reset(&req(&id, vec![], "RCA"), T0 + 1).is_err(),
            "{lvl:?}"
        );
        assert_eq!(ks.effective_level(), lvl);
    }
}

#[test]
fn one_person_cannot_fill_two_seats_or_be_counted_twice() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    let id = trigger_id(ks.trigger(L::KillLevelLogic as i32, "x", "m", T0).unwrap());
    let dup = vec![approval("a", Role::Operator), approval("a", Role::Operator)];
    assert!(ks.reset(&req(&id, dup, "RCA"), T0 + 1).is_err());
    let mut ks = KillSwitch::fresh(T0, cfg());
    let id = trigger_id(ks.trigger(L::KillLevelHard as i32, "x", "m", T0).unwrap());
    // "a" tries to be both an operator and the compliance officer
    let both = vec![
        approval("a", Role::Operator),
        approval("a", Role::Compliance),
        approval("b", Role::Operator),
    ];
    assert!(ks.reset(&req(&id, both, "RCA"), T0 + 1).is_err());
}

#[test]
fn logic_hard_and_physical_resets_need_a_root_cause_reference() {
    for (lvl, apps) in [
        (
            L::KillLevelLogic,
            vec![approval("a", Role::Operator), approval("b", Role::Operator)],
        ),
        (
            L::KillLevelHard,
            vec![
                approval("a", Role::Operator),
                approval("b", Role::Operator),
                approval("c", Role::Compliance),
            ],
        ),
        (
            L::KillLevelPhysical,
            vec![
                approval("a", Role::Operator),
                approval("b", Role::Operator),
                approval("c", Role::Compliance),
            ],
        ),
    ] {
        let mut ks = KillSwitch::fresh(T0, cfg());
        let id = trigger_id(ks.trigger(lvl as i32, "x", "m", T0).unwrap());
        assert_eq!(
            ks.reset(&req(&id, apps.clone(), "  "), T0 + 1),
            Err(ResetRefusal::RootCauseRequired)
        );
        assert!(ks.reset(&req(&id, apps, "RCA-9"), T0 + 1).is_ok());
    }
}

#[test]
fn dead_mans_reset_needs_a_heartbeat_after_the_latch_plus_one_operator() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    ks.tick(T0 + 5 * HOUR_NS);
    let id = ks.state().latches[0].trigger_id.clone();
    let ops = vec![approval("a", Role::Operator)];
    assert_eq!(
        ks.reset(&req(&id, ops.clone(), ""), T0 + 5 * HOUR_NS + 1),
        Err(ResetRefusal::HeartbeatRequired)
    );
    ks.heartbeat("a", T0 + 5 * HOUR_NS + 2);
    assert!(
        ks.reset(&req(&id, vec![], ""), T0 + 5 * HOUR_NS + 3)
            .is_err(),
        "still needs an operator"
    );
    assert!(ks.reset(&req(&id, ops, ""), T0 + 5 * HOUR_NS + 3).is_ok());
    assert_eq!(ks.effective_level(), L::KillLevelNormal);
}

#[test]
fn reset_of_unknown_trigger_is_refused() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    assert_eq!(
        ks.reset(&req("nope", vec![approval("a", Role::Operator)], ""), T0),
        Err(ResetRefusal::UnknownTrigger)
    );
}

#[test]
fn latch_cap_is_enforced_without_lowering_protection() {
    let mut ks = KillSwitch::fresh(T0, cfg());
    for _ in 0..MAX_LATCHES {
        ks.trigger(L::KillLevelSoft as i32, "x", "m", T0).unwrap();
    }
    assert!(ks.trigger(L::KillLevelSoft as i32, "x", "m", T0).is_ok());
    assert_eq!(ks.state().latches.len(), MAX_LATCHES);
    assert_eq!(
        ks.trigger(L::KillLevelHard as i32, "x", "m", T0),
        Err(KillError::TooManyLatches)
    );
}

// ---- controller: persistence, fail-closed, audit, watch ----
struct Rig {
    store: Arc<MemoryKillStore>,
    audit: Arc<MemorySink>,
    clock: ManualClock,
    ctl: KillController,
}

fn rig_with(store: Arc<MemoryKillStore>, mandatory: bool) -> Rig {
    let audit = Arc::new(MemorySink::default());
    let clock = ManualClock::new(T0);
    let ctl = KillController::start(
        store.clone(),
        audit.clone(),
        Arc::new(clock.clone()),
        cfg(),
        mandatory,
    );
    Rig {
        store,
        audit,
        clock,
        ctl,
    }
}

fn rig() -> Rig {
    rig_with(Arc::new(MemoryKillStore::default()), true)
}

#[test]
fn fresh_start_is_normal_and_persists_the_initial_state() {
    let r = rig();
    assert_eq!(r.ctl.effective_level(), Some(L::KillLevelNormal));
    assert!(r.store.saved().is_some());
}

// ---- invariant 3: a restart never lowers the level; unreadable => HARD ----
#[test]
fn restart_preserves_a_tripped_latch() {
    let r = rig();
    r.ctl
        .trigger(L::KillLevelLogic as i32, "drawdown", "aegis/drawdown")
        .unwrap();
    let restarted = rig_with(r.store.clone(), true);
    assert_eq!(restarted.ctl.effective_level(), Some(L::KillLevelLogic));
    assert_eq!(restarted.ctl.state().latches.len(), 1);
}

#[test]
fn unreadable_state_starts_at_hard_and_is_audited() {
    let store = Arc::new(MemoryKillStore::default());
    store.set_unreadable(true);
    let r = rig_with(store.clone(), true);
    assert_eq!(r.ctl.effective_level(), Some(L::KillLevelHard));
    assert_eq!(r.ctl.state().latches[0].actor_id, STARTUP_ACTOR);
    assert!(r
        .audit
        .events()
        .iter()
        .any(|e| matches!(e, AuditEvent::KillTransition(k) if k.kind == "startup_fail_closed")));
    // the HARD latch was persisted, so a readable restart still sees it
    store.set_unreadable(false);
    assert_eq!(
        rig_with(store, true).ctl.effective_level(),
        Some(L::KillLevelHard)
    );
}

#[test]
fn a_store_that_cannot_save_at_startup_forces_hard() {
    let store = Arc::new(MemoryKillStore::default());
    store.set_fail_saves(true);
    assert_eq!(
        rig_with(store, true).ctl.effective_level(),
        Some(L::KillLevelHard)
    );
}

#[test]
fn trip_that_cannot_be_persisted_still_latches_and_forces_hard() {
    let r = rig();
    r.store.set_fail_saves(true);
    let err = r
        .ctl
        .trigger(L::KillLevelSoft as i32, "x", "op")
        .unwrap_err();
    assert_eq!(err, ControllerError::NotPersisted);
    assert_eq!(r.ctl.effective_level(), Some(L::KillLevelHard));
    assert!(r
        .ctl
        .state()
        .latches
        .iter()
        .any(|l| l.actor_id == STORE_FAILURE_ACTOR));
}

#[test]
fn poisoned_state_reads_as_hard_never_normal() {
    let r = rig();
    let ctl = Arc::new(r.ctl);
    let c2 = ctl.clone();
    let _ = std::thread::spawn(move || {
        let _g = c2.inner_for_test().lock().unwrap();
        panic!("poison the lock");
    })
    .join();
    assert_eq!(ctl.effective_level(), None);
    assert_eq!(ctl.state().effective_level, L::KillLevelHard as i32);
}

#[test]
fn controller_reset_audits_persists_and_publishes() {
    let r = rig();
    let mut rx = r.ctl.subscribe();
    let st = r.ctl.trigger(L::KillLevelSoft as i32, "x", "op").unwrap();
    assert!(rx.has_changed().unwrap());
    let id = st.latches[0].trigger_id.clone();
    let out = r
        .ctl
        .reset(
            &req(&id, vec![approval("alice", Role::Operator)], ""),
            "peer-1",
        )
        .unwrap();
    assert_eq!(out.effective_level, L::KillLevelNormal as i32);
    assert_eq!(r.store.saved().unwrap().latches.len(), 0);
    assert_eq!(
        rx.borrow_and_update().effective_level,
        L::KillLevelNormal as i32
    );
    let ev = r.audit.events();
    assert!(ev.iter().any(|e| matches!(e, AuditEvent::KillTransition(k) if k.kind == "reset" && k.actor_id == "alice" && k.prev_level == "KILL_LEVEL_SOFT" && k.new_level == "KILL_LEVEL_NORMAL")));
}

#[test]
fn refused_reset_is_audited_and_changes_nothing() {
    let r = rig();
    let st = r.ctl.trigger(L::KillLevelHard as i32, "x", "op").unwrap();
    let id = st.latches[0].trigger_id.clone();
    let seq = r.ctl.state_seq();
    let err = r
        .ctl
        .reset(
            &req(&id, vec![approval("alice", Role::Operator)], "RCA"),
            "peer-1",
        )
        .unwrap_err();
    assert!(matches!(err, ResetRefusal::InsufficientApprovals { .. }));
    assert_eq!(r.ctl.effective_level(), Some(L::KillLevelHard));
    assert_eq!(r.ctl.state_seq(), seq);
    assert!(r
        .audit
        .events()
        .iter()
        .any(|e| matches!(e, AuditEvent::ResetRefused { caller, .. } if caller == "peer-1")));
}

#[test]
fn reset_is_refused_when_it_cannot_be_persisted() {
    let r = rig();
    let st = r.ctl.trigger(L::KillLevelSoft as i32, "x", "op").unwrap();
    let id = st.latches[0].trigger_id.clone();
    r.store.set_fail_saves(true);
    let err = r
        .ctl
        .reset(&req(&id, vec![approval("a", Role::Operator)], ""), "p")
        .unwrap_err();
    assert_eq!(err, ResetRefusal::StoreUnavailable);
    assert_eq!(r.ctl.effective_level(), Some(L::KillLevelSoft));
}

#[test]
fn reset_is_refused_when_a_mandatory_audit_sink_fails_but_allowed_when_optional() {
    let store = Arc::new(MemoryKillStore::default());
    let mk = |mandatory| {
        KillController::start(
            store.clone(),
            Arc::new(FailingSink),
            Arc::new(ManualClock::new(T0)),
            cfg(),
            mandatory,
        )
    };
    let ctl = mk(true);
    let st = ctl.trigger(L::KillLevelSoft as i32, "x", "op").unwrap();
    let id = st.latches[0].trigger_id.clone();
    let r = req(&id, vec![approval("a", Role::Operator)], "");
    assert_eq!(ctl.reset(&r, "p"), Err(ResetRefusal::AuditUnavailable));
    assert_eq!(ctl.effective_level(), Some(L::KillLevelSoft));
    let optional = mk(false);
    assert!(optional.reset(&r, "p").is_ok());
}

#[test]
fn controller_tick_trips_dead_mans_and_heartbeat_is_published() {
    let r = rig();
    let rx = r.ctl.subscribe();
    r.clock.advance_ms(4 * 3_600_000 + 1);
    r.ctl.tick();
    assert_eq!(r.ctl.effective_level(), Some(L::KillLevelDeadMans));
    assert!(rx.has_changed().unwrap());
    let st = r.ctl.heartbeat("op-1");
    assert_eq!(st.last_operator_heartbeat_ns, T0 + 4 * HOUR_NS + 1_000_000);
    assert_eq!(
        st.operator_heartbeat_deadline_ns,
        st.last_operator_heartbeat_ns + 4 * HOUR_NS
    );
    assert_eq!(st.effective_level, L::KillLevelDeadMans as i32);
}

#[test]
fn state_store_trait_object_is_usable() {
    let _s: Arc<dyn KillStore> = Arc::new(MemoryKillStore::default());
}
