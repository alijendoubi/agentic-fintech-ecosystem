//! Kill-switch hierarchy (spec section 5): a latch state machine.
//!
//! * Each trigger is an independent latch with its own `trigger_id`.
//! * Effective level = max level over active latches (pure function).
//! * Automatic transitions only go UP; lowering requires an authenticated
//!   reset by the authority for the latch's level (see [`reset`]).
//! * `KILL_LEVEL_NORMAL` is the proto zero value, so a trigger with level 0 or
//!   an unknown level is a malformed request and latches HARD, never NORMAL.
//! * Unknown levels found in persisted state count as HARD.
//!
//! This module is pure (time is injected, no I/O); persistence and fan-out
//! live in [`store`] and [`controller`].

pub mod controller;
pub mod reset;
pub mod store;
pub mod watchdog;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::pb::KillSwitchLevel;

pub use reset::{ResetRefusal, ResetRequest, Role, VerifiedApproval};

/// Hard cap on simultaneously active latches (bounded memory).
pub const MAX_LATCHES: usize = 1_024;
/// Actor id of the automatic operator-heartbeat (dead-man's) latch.
pub const HEARTBEAT_ACTOR: &str = "aegis/operator-heartbeat";
/// Actor id used when the persisted state cannot be read at start-up.
pub const STARTUP_ACTOR: &str = "aegis/startup";
/// Actor id used when a trip could not be persisted.
pub const STORE_FAILURE_ACTOR: &str = "aegis/state-store";
const NANOS_PER_MS: i64 = 1_000_000;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum KillError {
    #[error("too many active latches")]
    TooManyLatches,
}

/// One active latch. `level` is the wire integer so persisted data with an
/// unknown value can be loaded and treated as HARD instead of being lost.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Latch {
    pub trigger_id: String,
    pub level: i32,
    pub reason: String,
    pub actor_id: String,
    pub latched_at_ns: i64,
}

impl Latch {
    /// Normalised level: unknown wire values are HARD (fail closed).
    pub fn level_enum(&self) -> KillSwitchLevel {
        KillSwitchLevel::from_wire(self.level).unwrap_or(KillSwitchLevel::KillLevelHard)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KillState {
    pub latches: Vec<Latch>,
    pub state_seq: u64,
    pub updated_at_ns: i64,
    pub last_operator_heartbeat_ns: i64,
}

/// Heartbeat timing (spec 5.2/5.3), derived from the limits file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HeartbeatConfig {
    pub interval_ns: i64,
    pub warn_ns: i64,
}

impl HeartbeatConfig {
    pub fn from_ms(interval_ms: u64, warn_ms: u64) -> HeartbeatConfig {
        let to_ns = |ms: u64| {
            i64::try_from(ms)
                .unwrap_or(i64::MAX / NANOS_PER_MS)
                .saturating_mul(NANOS_PER_MS)
        };
        HeartbeatConfig {
            interval_ns: to_ns(interval_ms),
            warn_ns: to_ns(warn_ms),
        }
    }
}

/// What a state-machine operation did, for audit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KillEvent {
    Latched {
        trigger_id: String,
        level: KillSwitchLevel,
        reason: String,
        actor_id: String,
    },
    Reset {
        trigger_id: String,
        level: KillSwitchLevel,
        approvers: Vec<String>,
    },
    Heartbeat {
        operator_id: String,
    },
    HeartbeatWarning,
}

#[derive(Debug, Clone)]
pub struct KillSwitch {
    state: KillState,
    cfg: HeartbeatConfig,
    warned: bool,
}

fn level_rank(l: KillSwitchLevel) -> i32 {
    l as i32
}

impl KillSwitch {
    /// First boot: no latches, the heartbeat baseline is "now".
    pub fn fresh(now_ns: i64, cfg: HeartbeatConfig) -> KillSwitch {
        KillSwitch {
            state: KillState {
                latches: Vec::new(),
                state_seq: 1,
                updated_at_ns: now_ns,
                last_operator_heartbeat_ns: now_ns,
            },
            cfg,
            warned: false,
        }
    }

    /// Restore persisted state. A restart never lowers the level.
    pub fn from_persisted(mut state: KillState, cfg: HeartbeatConfig) -> KillSwitch {
        if state.last_operator_heartbeat_ns <= 0 {
            state.last_operator_heartbeat_ns = state.updated_at_ns;
        }
        KillSwitch {
            state,
            cfg,
            warned: false,
        }
    }

    /// Persisted state missing or unreadable: start at HARD (spec 5.1).
    pub fn fail_closed(now_ns: i64, cfg: HeartbeatConfig, why: &str) -> KillSwitch {
        let mut ks = KillSwitch::fresh(now_ns, cfg);
        let _ = ks.latch(
            KillSwitchLevel::KillLevelHard,
            format!("start-up fail-closed: {why}"),
            STARTUP_ACTOR,
            now_ns,
        );
        ks
    }

    pub fn state(&self) -> &KillState {
        &self.state
    }

    /// Max level over all latches; NORMAL only when there are none.
    pub fn effective_level(&self) -> KillSwitchLevel {
        self.state
            .latches
            .iter()
            .map(Latch::level_enum)
            .max_by_key(|l| level_rank(*l))
            .unwrap_or(KillSwitchLevel::KillLevelNormal)
    }

    pub fn heartbeat_deadline_ns(&self) -> i64 {
        self.state
            .last_operator_heartbeat_ns
            .saturating_add(self.cfg.interval_ns)
    }

    fn bump(&mut self, now_ns: i64) {
        self.state.state_seq = self.state.state_seq.saturating_add(1);
        self.state.updated_at_ns = now_ns;
    }

    fn latch(
        &mut self,
        level: KillSwitchLevel,
        reason: String,
        actor: &str,
        now_ns: i64,
    ) -> Result<KillEvent, KillError> {
        if self.state.latches.len() >= MAX_LATCHES
            && level_rank(level) <= level_rank(self.effective_level())
        {
            // At the cap a lower-or-equal trip adds no protection; report the
            // current state instead of growing without bound.
            return Ok(KillEvent::Latched {
                trigger_id: String::new(),
                level: self.effective_level(),
                reason,
                actor_id: actor.to_owned(),
            });
        }
        if self.state.latches.len() >= MAX_LATCHES {
            return Err(KillError::TooManyLatches);
        }
        let trigger_id = uuid::Uuid::new_v4().to_string();
        self.state.latches.push(Latch {
            trigger_id: trigger_id.clone(),
            level: level as i32,
            reason: reason.clone(),
            actor_id: actor.to_owned(),
            latched_at_ns: now_ns,
        });
        self.bump(now_ns);
        Ok(KillEvent::Latched {
            trigger_id,
            level,
            reason,
            actor_id: actor.to_owned(),
        })
    }

    /// Latch a trigger. Level 0 (NORMAL / unset) and unknown values are
    /// malformed and latch HARD: the request cannot be understood, and doing
    /// nothing would fail open.
    pub fn trigger(
        &mut self,
        raw_level: i32,
        reason: &str,
        actor: &str,
        now_ns: i64,
    ) -> Result<KillEvent, KillError> {
        let (level, reason) = match KillSwitchLevel::from_wire(raw_level) {
            Some(l) if l != KillSwitchLevel::KillLevelNormal => (l, reason.to_owned()),
            _ => (
                KillSwitchLevel::KillLevelHard,
                format!("malformed trigger level {raw_level}: {reason}"),
            ),
        };
        self.latch(level, reason, actor, now_ns)
    }

    /// Operator heartbeat: proves a human is present. Extends the deadline; it
    /// does NOT clear a latched DEAD_MANS (that needs a reset).
    pub fn heartbeat(&mut self, operator_id: &str, now_ns: i64) -> KillEvent {
        self.state.last_operator_heartbeat_ns = now_ns;
        self.warned = false;
        self.bump(now_ns);
        KillEvent::Heartbeat {
            operator_id: operator_id.to_owned(),
        }
    }

    /// Periodic evaluation: dead-man's trip and the pre-trip warning.
    pub fn tick(&mut self, now_ns: i64) -> Vec<KillEvent> {
        let mut events = Vec::new();
        let lapsed = now_ns > self.heartbeat_deadline_ns();
        let already = self
            .state
            .latches
            .iter()
            .any(|l| l.actor_id == HEARTBEAT_ACTOR);
        if lapsed && !already {
            let reason = "operator heartbeat missed".to_owned();
            if let Ok(ev) = self.latch(
                KillSwitchLevel::KillLevelDeadMans,
                reason,
                HEARTBEAT_ACTOR,
                now_ns,
            ) {
                events.push(ev);
            }
        }
        let warn_at = self
            .state
            .last_operator_heartbeat_ns
            .saturating_add(self.cfg.warn_ns);
        if !lapsed && !self.warned && now_ns > warn_at {
            self.warned = true;
            events.push(KillEvent::HeartbeatWarning);
        }
        events
    }

    /// Apply a reset if the authority rules are satisfied (see [`reset`]).
    pub fn reset(&mut self, req: &ResetRequest, now_ns: i64) -> Result<KillEvent, ResetRefusal> {
        let latch = self
            .state
            .latches
            .iter()
            .find(|l| l.trigger_id == req.trigger_id)
            .ok_or(ResetRefusal::UnknownTrigger)?;
        let level = latch.level_enum();
        reset::authorize(
            level,
            latch.latched_at_ns,
            self.state.last_operator_heartbeat_ns,
            req,
        )?;
        self.state
            .latches
            .retain(|l| l.trigger_id != req.trigger_id);
        self.bump(now_ns);
        let mut approvers: Vec<String> = req
            .approvals
            .iter()
            .map(|a| a.approver_id.clone())
            .collect();
        approvers.sort();
        approvers.dedup();
        Ok(KillEvent::Reset {
            trigger_id: req.trigger_id.clone(),
            level,
            approvers,
        })
    }
}

#[cfg(test)]
mod tests;
