//! Held signals (spec 4.6): creation, expiry and `ResolveHold`.
//!
//! A human can release a SOFT block but never a hard one: release re-runs
//! every hard control against CURRENT state (`Mode::Release`). On expiry with
//! no decision the signal is rejected (fail closed).

use prost::Message;
use thiserror::Error;

use super::build::{base, decision_audit, downgrade, rejected};
use super::{Core, Engine};
use crate::audit::AuditEvent;
use crate::controls::ReplayVerdict;
use crate::domain::{Mode, ValidatedSignal};
use crate::pb::{
    AegisDecision, DecisionStatus, ReasonCode, ResolveHoldRequest, SignalStatus, TradeSignal,
};
use crate::state::holds::Held;
use crate::state::replay::ReplayRecord;

const NANOS_PER_MS: i64 = 1_000_000;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum HoldError {
    #[error("unknown or expired hold_id")]
    NotFound,
    /// The release request itself is not acceptable; the hold stays pending.
    #[error("hold release refused: {0}")]
    Refused(&'static str),
    #[error("internal error")]
    Internal,
}

impl Engine {
    /// Turn a soft-blocked evaluation into a held decision.
    pub(super) fn hold(
        &self,
        core: &mut Core,
        mut d: AegisDecision,
        raw: &TradeSignal,
        v: &ValidatedSignal,
        hash: [u8; 32],
        now: i64,
    ) -> AegisDecision {
        let window_ms = i64::try_from(self.deps.limits.config.timings.hold_window_ms)
            .unwrap_or(i64::MAX / NANOS_PER_MS);
        let expires = v
            .valid_until_ns
            .min(now.saturating_add(window_ms.saturating_mul(NANOS_PER_MS)));
        let hold_id = uuid::Uuid::new_v4().to_string();
        let held = Held {
            hold_id: hold_id.clone(),
            signal: raw.clone(),
            validated: v.clone(),
            payload_sha256: hash,
            expires_at_ns: expires,
        };
        if expires <= now || !core.holds.insert(held) {
            downgrade(
                &mut d,
                ReasonCode::ReasonStateUnavailable,
                "HOLD",
                "cannot hold the signal",
            );
            return d;
        }
        d.hold_id = hold_id;
        d.hold_expires_at_ns = expires;
        d
    }

    /// Record an expired hold as a REJECT (idempotent replay record + audit).
    pub(super) fn expire_hold(&self, held: &Held, now: i64) {
        let d = rejected(
            &held.validated.signal_id,
            now,
            self.deps.kill.state_seq(),
            ReasonCode::ReasonHoldExpired,
            "HOLD",
            "hold expired without a decision",
        );
        let rec = ReplayRecord::new(&d.signal_id, &held.payload_sha256, &d.encode_to_vec(), now);
        if let Err(e) = self.deps.replay.record(&rec) {
            tracing::error!(error = %e, "could not record hold expiry");
        }
        self.audit_hold(&held.hold_id, &d.signal_id, "", "", false, "expired", now);
        let sha = &self.deps.limits.sha256_hex;
        let _ = self
            .deps
            .audit
            .record(&decision_audit(&d, &held.validated.symbol, sha));
    }

    #[allow(clippy::too_many_arguments)]
    fn audit_hold(
        &self,
        hold_id: &str,
        signal_id: &str,
        op: &str,
        second: &str,
        approved: bool,
        outcome: &str,
        now: i64,
    ) {
        let event = AuditEvent::HoldResolved {
            hold_id: hold_id.to_owned(),
            signal_id: signal_id.to_owned(),
            operator_id: op.to_owned(),
            second_approver_id: second.to_owned(),
            approved,
            outcome: outcome.to_owned(),
            at_ns: now,
        };
        if let Err(e) = self.deps.audit.record(&event) {
            tracing::error!(error = %e, "audit sink failed for a hold resolution");
        }
    }

    fn check_release_request(&self, req: &ResolveHoldRequest) -> Result<(), HoldError> {
        let cfg = &self.deps.limits.config;
        if req.operator_id.trim().is_empty() {
            return Err(HoldError::Refused("operator_id required"));
        }
        if !req.approve {
            return Ok(());
        }
        if cfg.hold_requires_second_approver
            && (req.second_approver_id.trim().is_empty()
                || req.second_approver_id == req.operator_id)
        {
            return Err(HoldError::Refused("a distinct second approver is required"));
        }
        if cfg.hold_requires_cooling_period && !req.cooling_period_enforced {
            return Err(HoldError::Refused("cooling period must be enforced"));
        }
        let s = req.reverse_guardrail_distress_score;
        if !s.is_finite() || !(0.0..=1.0).contains(&s) {
            return Err(HoldError::Refused(
                "distress score must be finite and in [0, 1]",
            ));
        }
        if s >= cfg.hold_max_distress_score {
            return Err(HoldError::Refused(
                "distress score at or above the release limit",
            ));
        }
        Ok(())
    }

    /// Human decision on a held signal. `req.operator_id` must already have
    /// been bound to the authenticated caller by the gRPC layer.
    pub fn resolve_hold(&self, req: &ResolveHoldRequest) -> Result<AegisDecision, HoldError> {
        let now = self.now().ok_or(HoldError::Internal)?;
        let mut core = self.lock().ok_or(HoldError::Internal)?;
        for held in core.holds.take_expired(now) {
            self.expire_hold(&held, now);
        }
        if core.holds.get(&req.hold_id).is_none() {
            return Err(HoldError::NotFound);
        }
        self.check_release_request(req)?;
        let held = core.holds.take(&req.hold_id).ok_or(HoldError::NotFound)?;
        let d = if req.approve {
            self.release(&mut core, &held, now)
        } else {
            self.reject_by_operator(&mut core, &held, now)
        };
        let outcome = format!(
            "{:?}",
            DecisionStatus::try_from(d.decision).unwrap_or(DecisionStatus::DecisionUnspecified)
        );
        self.audit_hold(
            &req.hold_id,
            &d.signal_id,
            &req.operator_id,
            &req.second_approver_id,
            req.approve,
            &outcome,
            now,
        );
        Ok(d)
    }

    fn reject_by_operator(&self, core: &mut Core, held: &Held, now: i64) -> AegisDecision {
        let mut d = base(&held.validated.signal_id, now, self.deps.kill.state_seq());
        d.signal_status = SignalStatus::SignalRejectedHardBlock as i32;
        downgrade(
            &mut d,
            ReasonCode::ReasonHoldRejectedByOperator,
            "HOLD",
            "operator rejected the hold",
        );
        self.finalize(core, d, &held.validated.symbol, Some(&held.payload_sha256))
    }

    /// Release: re-run all hard controls now; soft controls are what the
    /// human just decided. An approval is signed like any other.
    fn release(&self, core: &mut Core, held: &Held, now: i64) -> AegisDecision {
        let mut d = self.decide(
            core,
            &held.signal,
            &held.validated,
            held.payload_sha256,
            now,
            Mode::Release,
            ReplayVerdict::New,
        );
        if d.decision == DecisionStatus::DecisionHeldForHuman as i32 {
            // Release mode has no soft controls; anything still soft is a bug.
            self.drop_hold(core, &d);
            downgrade(
                &mut d,
                ReasonCode::ReasonInternalError,
                "HOLD",
                "release produced a hold",
            );
        }
        d
    }
}
