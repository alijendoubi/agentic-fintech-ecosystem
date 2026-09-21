//! `SubmitSignal`: validate, replay-check, evaluate, then approve (sign +
//! reserve exposure), hold, or reject; audit; record for idempotency.

use prost::Message;

use super::build::{base, decision_audit, downgrade, from_eval, payload_hash, rejected};
use super::{CancelToken, Core, Engine};
use crate::audit::AuditEvent;
use crate::controls::{evaluate, Evaluation, ReplayVerdict};
use crate::domain::{Mode, ValidatedSignal};
use crate::hex;
use crate::pb::{
    AegisDecision, ControlResult, KillSwitchLevel, ReasonCode, SignalStatus, TradeSignal,
};
use crate::signing::attest::{attest_order, AttestParams};
use crate::signing::SignError;
use crate::state::replay::ReplayRecord;
use crate::validate::validate_signal;

const NANOS_PER_MS: i64 = 1_000_000;
const GAVE_UP: &str = "caller deadline exceeded; nothing was signed or reserved for it";

/// How one `SubmitSignal` attempt is evaluated.
#[derive(Clone, Copy)]
pub(super) struct Attempt<'a> {
    pub mode: Mode,
    pub verdict: ReplayVerdict,
    pub cancel: &'a CancelToken,
}

enum ReserveFail {
    /// The caller gave up before the reservation was committed.
    GaveUp,
    State(&'static str),
}

/// The (never delivered) result for a request the caller already abandoned.
fn abandoned(signal_id: &str, now_ns: i64, seq: u64) -> AegisDecision {
    rejected(
        signal_id,
        now_ns,
        seq,
        ReasonCode::ReasonInternalError,
        "CANCEL",
        GAVE_UP,
    )
}

impl Engine {
    /// Evaluate a signal against every pre-trade control. Fail closed.
    pub fn submit_signal(&self, raw: &TradeSignal) -> AegisDecision {
        self.submit_signal_cancellable(raw, &CancelToken::default())
    }

    /// As [`Engine::submit_signal`], but stops (before signing, and before a
    /// reservation is committed) once `cancel` is set: the caller has already
    /// been told DEADLINE_EXCEEDED, so an approval would be unused. A
    /// cancelled request leaves no replay record, so the id can be retried.
    pub fn submit_signal_cancellable(
        &self,
        raw: &TradeSignal,
        cancel: &CancelToken,
    ) -> AegisDecision {
        let seq = self.deps.kill.state_seq();
        if cancel.is_cancelled() {
            return abandoned(&raw.signal_id, 0, seq);
        }
        let Some(now) = self.now() else {
            return rejected(
                &raw.signal_id,
                0,
                seq,
                ReasonCode::ReasonInternalError,
                "CLOCK",
                "clock unavailable",
            );
        };
        let accept_legacy = self.deps.limits.config.accept_legacy_double_fields;
        let validated = match validate_signal(raw, accept_legacy) {
            Ok(v) => v,
            Err(c02) => return self.reject_invalid(raw, *c02, now, seq),
        };
        let hash = payload_hash(raw);
        let Some(mut core) = self.lock() else {
            return rejected(
                &raw.signal_id,
                now,
                seq,
                ReasonCode::ReasonInternalError,
                "LOCK",
                "engine state poisoned",
            );
        };
        // Waiting for the engine lock can outlast the caller's deadline.
        if cancel.is_cancelled() {
            return abandoned(&raw.signal_id, now, seq);
        }
        let verdict = match self.deps.replay.lookup(&validated.signal_id) {
            Ok(Some(rec)) => match self.replay_hit(&rec, &hash, &validated, now) {
                Ok(original) => return original,
                Err(v) => v,
            },
            Ok(None) => ReplayVerdict::New,
            Err(e) => {
                tracing::error!(error = %e, "replay store lookup failed");
                ReplayVerdict::StoreUnavailable
            }
        };
        let ctx = Attempt {
            mode: Mode::Submit,
            verdict,
            cancel,
        };
        self.decide(&mut core, raw, &validated, hash, now, ctx)
    }

    fn reject_invalid(
        &self,
        raw: &TradeSignal,
        c02: ControlResult,
        now: i64,
        seq: u64,
    ) -> AegisDecision {
        let mut d = base(&raw.signal_id, now, seq);
        d.results = vec![c02];
        downgrade_keep_results(&mut d);
        let audit = decision_audit(&d, &raw.symbol, &self.deps.limits.sha256_hex);
        if let Err(e) = self.deps.audit.record(&audit) {
            tracing::error!(error = %e, "audit sink failed for an invalid signal");
        }
        d
    }

    /// Same id: identical payload returns the ORIGINAL decision (never a new
    /// approval or attestation); a different payload is a replay attack.
    fn replay_hit(
        &self,
        rec: &ReplayRecord,
        hash: &[u8; 32],
        v: &ValidatedSignal,
        now: i64,
    ) -> Result<AegisDecision, ReplayVerdict> {
        if rec.payload_sha256 != hex::encode(hash) {
            let event = AuditEvent::Security {
                kind: "replay_payload_mismatch".into(),
                detail: format!("signal_id {}", v.signal_id),
                at_ns: now,
            };
            if let Err(e) = self.deps.audit.record(&event) {
                tracing::error!(error = %e, "audit sink failed for a security event");
            }
            return Err(ReplayVerdict::PayloadMismatch);
        }
        let original =
            hex::decode(&rec.decision_hex).and_then(|b| AegisDecision::decode(b.as_slice()).ok());
        Ok(original.unwrap_or_else(|| {
            rejected(
                &v.signal_id,
                now,
                0,
                ReasonCode::ReasonDuplicateSignal,
                "C07",
                "stored decision unreadable",
            )
        }))
    }

    /// Shared by `SubmitSignal` (Mode::Submit) and hold release (Mode::Release).
    pub(super) fn decide(
        &self,
        core: &mut Core,
        raw: &TradeSignal,
        v: &ValidatedSignal,
        hash: [u8; 32],
        now: i64,
        attempt: Attempt<'_>,
    ) -> AegisDecision {
        let seq = self.deps.kill.state_seq();
        let ev = {
            let snap = self.snapshot(core, v, now, attempt.mode, attempt.verdict);
            evaluate(v, &snap)
        };
        let mut d = from_eval(base(&v.signal_id, now, seq), &ev);
        let mut gave_up = false;
        match ev.decision {
            crate::pb::DecisionStatus::DecisionApproved => {
                (d, gave_up) = self.approve(core, d, v, &ev, now, attempt.cancel);
            }
            crate::pb::DecisionStatus::DecisionHeldForHuman => {
                d = self.hold(core, d, raw, v, hash, now)
            }
            _ => {}
        }
        // An abandoned attempt is not recorded, so the signal id stays usable.
        let record = (matches!(attempt.verdict, ReplayVerdict::New) && !gave_up).then_some(&hash);
        self.finalize(core, d, &v.symbol, record)
    }

    /// True if signing must not happen right now (missing state = HARD).
    fn kill_blocks_signing(&self) -> bool {
        self.deps
            .kill
            .effective_level()
            .is_none_or(|l| l as i32 >= KillSwitchLevel::KillLevelDeadMans as i32)
    }

    /// Sign and reserve. The flag is true when the caller gave up (`cancel`)
    /// so the attempt was abandoned and nothing was left behind.
    fn approve(
        &self,
        core: &mut Core,
        mut d: AegisDecision,
        v: &ValidatedSignal,
        ev: &Evaluation,
        now: i64,
        cancel: &CancelToken,
    ) -> (AegisDecision, bool) {
        let Some(price) = ev.order_price else {
            downgrade(
                &mut d,
                ReasonCode::ReasonInternalError,
                "SIGN",
                "no bounded order price",
            );
            return (d, false);
        };
        if self.kill_blocks_signing() {
            downgrade(
                &mut d,
                ReasonCode::ReasonKillSwitchActive,
                "SIGN",
                "kill switch blocks signing",
            );
            return (d, false);
        }
        if cancel.is_cancelled() {
            downgrade(&mut d, ReasonCode::ReasonInternalError, "CANCEL", GAVE_UP);
            return (d, true);
        }
        let ttl_ms = i64::try_from(self.deps.limits.config.timings.attestation_ttl_ms)
            .unwrap_or(i64::MAX / NANOS_PER_MS);
        let params = AttestParams {
            signal: v,
            price,
            decided_at_ns: now,
            ttl_ns: ttl_ms.saturating_mul(NANOS_PER_MS),
            state_seq: d.aegis_state_seq,
            limits_sha256: &self.deps.limits.sha256_hex,
        };
        let (order, attestation) = match attest_order(&*self.deps.signer, &params) {
            Ok(pair) => pair,
            Err(SignError::Unavailable(why)) => {
                tracing::error!(error = %why, "signer unavailable");
                downgrade(
                    &mut d,
                    ReasonCode::ReasonHsmUnavailable,
                    "SIGN",
                    "signer unavailable",
                );
                return (d, false);
            }
            Err(e) => {
                tracing::error!(error = %e, "attestation failed");
                downgrade(
                    &mut d,
                    ReasonCode::ReasonInternalError,
                    "SIGN",
                    "attestation failed",
                );
                return (d, false);
            }
        };
        match self.reserve(
            core,
            v,
            &order.order_id,
            attestation.expires_at_ns,
            now,
            cancel,
        ) {
            Ok(()) => {}
            Err(ReserveFail::GaveUp) => {
                downgrade(&mut d, ReasonCode::ReasonInternalError, "CANCEL", GAVE_UP);
                return (d, true);
            }
            Err(ReserveFail::State(why)) => {
                downgrade(&mut d, ReasonCode::ReasonStateUnavailable, "STATE", why);
                return (d, false);
            }
        }
        // The (slow) persist may have outlasted the caller's deadline: nobody
        // will use this attestation, so give the exposure back.
        if cancel.is_cancelled() {
            self.release_abandoned(core, &order.order_id);
            downgrade(&mut d, ReasonCode::ReasonInternalError, "CANCEL", GAVE_UP);
            return (d, true);
        }
        d.order = Some(order);
        d.attestation = Some(attestation);
        (d, false)
    }

    /// Reserve exposure and record the order size; commit only if persisted
    /// (and only if the caller has not given up in the meantime).
    fn reserve(
        &self,
        core: &mut Core,
        v: &ValidatedSignal,
        order_id: &str,
        expires_at_ns: i64,
        now: i64,
        cancel: &CancelToken,
    ) -> Result<(), ReserveFail> {
        let Some(current) = core.portfolio.as_ref() else {
            return Err(ReserveFail::State("portfolio state unavailable"));
        };
        let mut candidate = current.clone();
        candidate.expire_reservations(now);
        candidate
            .reserve(order_id, &v.symbol, v.side.is_sell(), v.qty, expires_at_ns)
            .map_err(|_| ReserveFail::State("too many open orders"))?;
        candidate.record_size(&v.symbol, v.qty, now);
        if cancel.is_cancelled() {
            return Err(ReserveFail::GaveUp);
        }
        self.deps
            .portfolio_store
            .save(&candidate)
            .map_err(|_| ReserveFail::State("portfolio state could not be persisted"))?;
        core.portfolio = Some(candidate);
        Ok(())
    }

    /// Undo a committed reservation for an attestation nobody received. If the
    /// release cannot be persisted the reservation stays (fail closed: it
    /// simply expires with the attestation).
    fn release_abandoned(&self, core: &mut Core, order_id: &str) {
        let Some(current) = core.portfolio.as_ref() else {
            return;
        };
        let mut candidate = current.clone();
        if !candidate.release_unsubmitted(order_id) {
            return;
        }
        match self.deps.portfolio_store.save(&candidate) {
            Ok(()) => core.portfolio = Some(candidate),
            Err(e) => {
                tracing::error!(error = %e, "could not persist release of an abandoned reservation; it expires with the attestation")
            }
        }
    }

    /// Audit (fail closed if mandatory), then record for idempotency.
    pub(super) fn finalize(
        &self,
        core: &mut Core,
        mut d: AegisDecision,
        symbol: &str,
        record_hash: Option<&[u8; 32]>,
    ) -> AegisDecision {
        let sha = &self.deps.limits.sha256_hex;
        if let Err(e) = self.deps.audit.record(&decision_audit(&d, symbol, sha)) {
            tracing::error!(error = %e, "audit sink failed");
            if self.deps.limits.config.audit_mandatory {
                self.drop_hold(core, &d);
                downgrade(
                    &mut d,
                    ReasonCode::ReasonAuditUnavailable,
                    "AUDIT",
                    "audit sink unavailable",
                );
            }
        }
        let Some(hash) = record_hash else { return d };
        let rec = ReplayRecord::new(&d.signal_id, hash, &d.encode_to_vec(), d.decided_at_ns);
        if let Err(e) = self.deps.replay.record(&rec) {
            tracing::error!(error = %e, "replay record failed");
            self.drop_hold(core, &d);
            downgrade(
                &mut d,
                ReasonCode::ReasonStateUnavailable,
                "STATE",
                "replay state could not be recorded",
            );
            // best effort: the audit trail must show the final (rejected) outcome
            let _ = self.deps.audit.record(&decision_audit(&d, symbol, sha));
        }
        d
    }

    pub(super) fn drop_hold(&self, core: &mut Core, d: &AegisDecision) {
        if !d.hold_id.is_empty() {
            core.holds.take(&d.hold_id);
        }
    }
}

/// A decision whose `results` already hold the failed control: mark it rejected
/// with the matching reasons without adding a pseudo-control.
fn downgrade_keep_results(d: &mut AegisDecision) {
    d.decision = crate::pb::DecisionStatus::DecisionRejected as i32;
    d.signal_status = SignalStatus::SignalRejectedHardBlock as i32;
    d.reasons = d
        .results
        .iter()
        .filter(|r| !r.passed)
        .map(|r| r.reason)
        .collect();
}
