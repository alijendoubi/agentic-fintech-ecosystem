//! Thread-safe kill-switch owner: state machine + durable store + audit +
//! `WatchKillSwitchState` fan-out.
//!
//! Failure policy (fail closed, asymmetric):
//! * TRIPS are applied in memory first; if persisting fails an extra HARD latch
//!   is added (an Aegis that cannot remember its own state must not trade).
//! * RESETS are audited, then persisted, and only then committed in memory; any
//!   failure refuses the reset.
//! * A poisoned lock reads as HARD, never as NORMAL.

use std::sync::{Arc, Mutex, MutexGuard};

use thiserror::Error;
use tokio::sync::watch;

use super::store::{KillStore, LoadOutcome};
use super::{
    HeartbeatConfig, KillEvent, KillSwitch, ResetRefusal, ResetRequest, STORE_FAILURE_ACTOR,
};
use crate::audit::{AuditEvent, AuditSink, KillAudit};
use crate::clock::Clock;
use crate::pb::{self, KillSwitchLevel};

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ControllerError {
    #[error("trigger latched in memory but could not be persisted (state forced to HARD)")]
    NotPersisted,
}

pub struct KillController {
    inner: Mutex<KillSwitch>,
    store: Arc<dyn KillStore>,
    audit: Arc<dyn AuditSink>,
    clock: Arc<dyn Clock>,
    audit_mandatory: bool,
    tx: watch::Sender<pb::KillSwitchState>,
}

fn to_pb(ks: &KillSwitch) -> pb::KillSwitchState {
    let s = ks.state();
    pb::KillSwitchState {
        effective_level: ks.effective_level() as i32,
        latches: s
            .latches
            .iter()
            .map(|l| pb::LatchedTrigger {
                trigger_id: l.trigger_id.clone(),
                level: l.level_enum() as i32,
                reason: l.reason.clone(),
                actor_id: l.actor_id.clone(),
                latched_at_ns: l.latched_at_ns,
            })
            .collect(),
        state_seq: s.state_seq,
        updated_at_ns: s.updated_at_ns,
        last_operator_heartbeat_ns: s.last_operator_heartbeat_ns,
        operator_heartbeat_deadline_ns: ks.heartbeat_deadline_ns(),
    }
}

/// State reported when the lock is poisoned: HARD, never NORMAL.
fn hard_placeholder() -> pb::KillSwitchState {
    pb::KillSwitchState {
        effective_level: KillSwitchLevel::KillLevelHard as i32,
        ..pb::KillSwitchState::default()
    }
}

impl KillController {
    /// Load (or fail closed) and persist the initial state.
    pub fn start(
        store: Arc<dyn KillStore>,
        audit: Arc<dyn AuditSink>,
        clock: Arc<dyn Clock>,
        cfg: HeartbeatConfig,
        audit_mandatory: bool,
    ) -> KillController {
        let now = clock.now_ns().unwrap_or(0);
        let (mut ks, failed_closed) = match store.load() {
            LoadOutcome::Loaded(state) => (KillSwitch::from_persisted(state, cfg), None),
            LoadOutcome::Fresh => (KillSwitch::fresh(now, cfg), None),
            LoadOutcome::Unreadable(why) => {
                store.quarantine();
                (KillSwitch::fail_closed(now, cfg, &why), Some(why))
            }
        };
        if let Err(e) = store.save(ks.state()) {
            tracing::error!(error = %e, "initial kill-switch state could not be persisted; forcing HARD");
            ks.trigger(
                KillSwitchLevel::KillLevelHard as i32,
                "initial persist failed",
                STORE_FAILURE_ACTOR,
                now,
            );
        }
        let (tx, _rx) = watch::channel(to_pb(&ks));
        let controller = KillController {
            inner: Mutex::new(ks),
            store,
            audit,
            clock,
            audit_mandatory,
            tx,
        };
        if let Some(why) = failed_closed {
            controller.emit_startup_failure(&why, now);
        }
        controller
    }

    fn emit_startup_failure(&self, why: &str, now: i64) {
        let state = self.state();
        let audit = KillAudit {
            kind: "startup_fail_closed".into(),
            trigger_id: String::new(),
            actor_id: super::STARTUP_ACTOR.into(),
            reason: why.to_owned(),
            prev_level: "UNKNOWN".into(),
            new_level: format!("{:?}", state.effective_level),
            state_seq: state.state_seq,
            at_ns: now,
        };
        self.emit(&AuditEvent::KillTransition(audit));
    }

    fn lock(&self) -> Option<MutexGuard<'_, KillSwitch>> {
        self.inner.lock().ok()
    }

    fn now(&self) -> i64 {
        self.clock.now_ns().unwrap_or(0)
    }

    fn emit(&self, event: &AuditEvent) {
        if let Err(e) = self.audit.record(event) {
            tracing::error!(error = %e, "audit sink failed for a kill-switch record");
        }
    }

    /// Effective level, or `None` if state is unavailable (callers treat as HARD).
    pub fn effective_level(&self) -> Option<KillSwitchLevel> {
        self.lock().map(|ks| ks.effective_level())
    }

    pub fn state(&self) -> pb::KillSwitchState {
        self.lock().map_or_else(hard_placeholder, |ks| to_pb(&ks))
    }

    pub fn subscribe(&self) -> watch::Receiver<pb::KillSwitchState> {
        self.tx.subscribe()
    }

    /// Current heartbeat / liveness details for `GetAegisState`.
    pub fn state_seq(&self) -> u64 {
        self.state().state_seq
    }

    fn kill_audit(
        &self,
        kind: &str,
        ev: &KillEvent,
        prev: KillSwitchLevel,
        ks: &KillSwitch,
    ) -> AuditEvent {
        let (trigger_id, actor_id, reason) = match ev {
            KillEvent::Latched {
                trigger_id,
                reason,
                actor_id,
                evicted,
                ..
            } => {
                let reason = if evicted.is_empty() {
                    reason.clone()
                } else {
                    format!("{reason} [evicted latches: {}]", evicted.join(","))
                };
                (trigger_id.clone(), actor_id.clone(), reason)
            }
            KillEvent::Reset {
                trigger_id,
                approvers,
                ..
            } => (trigger_id.clone(), approvers.join(","), "reset".to_owned()),
            KillEvent::Heartbeat { operator_id } => {
                (String::new(), operator_id.clone(), "heartbeat".to_owned())
            }
            KillEvent::HeartbeatWarning => (
                String::new(),
                "aegis".into(),
                "operator heartbeat overdue soon".to_owned(),
            ),
        };
        AuditEvent::KillTransition(KillAudit {
            kind: kind.to_owned(),
            trigger_id,
            actor_id,
            reason,
            prev_level: prev.as_str_name().to_owned(),
            new_level: ks.effective_level().as_str_name().to_owned(),
            state_seq: ks.state().state_seq,
            at_ns: ks.state().updated_at_ns,
        })
    }

    /// Persist after a trip; on failure force HARD in memory.
    fn persist_or_force_hard(&self, ks: &mut KillSwitch, now: i64) -> bool {
        match self.store.save(ks.state()) {
            Ok(()) => true,
            Err(e) => {
                tracing::error!(error = %e, "kill-switch state not persisted; forcing HARD");
                ks.trigger(
                    KillSwitchLevel::KillLevelHard as i32,
                    "state store write failed",
                    STORE_FAILURE_ACTOR,
                    now,
                );
                false
            }
        }
    }

    /// Latch a trigger (`TriggerKillSwitch`).
    pub fn trigger(
        &self,
        raw_level: i32,
        reason: &str,
        actor: &str,
    ) -> Result<pb::KillSwitchState, ControllerError> {
        let now = self.now();
        let Some(mut ks) = self.lock() else {
            return Ok(hard_placeholder());
        };
        let prev = ks.effective_level();
        let ev = ks.trigger(raw_level, reason, actor, now);
        let persisted = self.persist_or_force_hard(&mut ks, now);
        let audit = self.kill_audit("latch", &ev, prev, &ks);
        let state = to_pb(&ks);
        drop(ks);
        self.emit(&audit);
        self.tx.send_replace(state.clone());
        if persisted {
            Ok(state)
        } else {
            Err(ControllerError::NotPersisted)
        }
    }

    /// Operator heartbeat.
    pub fn heartbeat(&self, operator_id: &str) -> pb::KillSwitchState {
        let now = self.now();
        let Some(mut ks) = self.lock() else {
            return hard_placeholder();
        };
        let prev = ks.effective_level();
        let ev = ks.heartbeat(operator_id, now);
        self.persist_or_force_hard(&mut ks, now);
        let audit = self.kill_audit("heartbeat", &ev, prev, &ks);
        let state = to_pb(&ks);
        drop(ks);
        self.emit(&audit);
        self.tx.send_replace(state.clone());
        state
    }

    /// Periodic tick (call about once a second): dead-man's trip and warning.
    pub fn tick(&self) {
        let now = self.now();
        let Some(mut ks) = self.lock() else { return };
        let prev = ks.effective_level();
        let events = ks.tick(now);
        if events.is_empty() {
            return;
        }
        self.persist_or_force_hard(&mut ks, now);
        let audits: Vec<AuditEvent> = events
            .iter()
            .map(|ev| {
                let kind = if matches!(ev, KillEvent::HeartbeatWarning) {
                    "heartbeat_warning"
                } else {
                    "latch"
                };
                self.kill_audit(kind, ev, prev, &ks)
            })
            .collect();
        let state = to_pb(&ks);
        drop(ks);
        for a in &audits {
            self.emit(a);
        }
        self.tx.send_replace(state);
    }

    /// Reset one latch. `caller` is the authenticated peer, for audit.
    pub fn reset(
        &self,
        req: &ResetRequest,
        caller: &str,
    ) -> Result<pb::KillSwitchState, ResetRefusal> {
        let now = self.now();
        let Some(mut ks) = self.lock() else {
            return Err(ResetRefusal::StoreUnavailable);
        };
        let prev = ks.effective_level();
        let mut candidate = ks.clone();
        let ev = match candidate.reset(req, now) {
            Ok(ev) => ev,
            Err(refusal) => {
                drop(ks);
                self.emit(&AuditEvent::ResetRefused {
                    trigger_id: req.trigger_id.clone(),
                    caller: caller.to_owned(),
                    reason: refusal.to_string(),
                    at_ns: now,
                });
                return Err(refusal);
            }
        };
        let audit = self.kill_audit("reset", &ev, prev, &candidate);
        if let Err(e) = self.audit.record(&audit) {
            tracing::error!(error = %e, "audit sink failed for a reset");
            if self.audit_mandatory {
                return Err(ResetRefusal::AuditUnavailable);
            }
        }
        if let Err(e) = self.store.save(candidate.state()) {
            tracing::error!(error = %e, "reset could not be persisted; refused");
            return Err(ResetRefusal::StoreUnavailable);
        }
        *ks = candidate;
        let state = to_pb(&ks);
        drop(ks);
        self.tx.send_replace(state.clone());
        Ok(state)
    }
}

impl std::fmt::Debug for KillController {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("KillController").finish_non_exhaustive()
    }
}

#[cfg(test)]
impl KillController {
    pub(super) fn inner_for_test(&self) -> &Mutex<KillSwitch> {
        &self.inner
    }
}
