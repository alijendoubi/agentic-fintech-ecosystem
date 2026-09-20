//! Decision construction and audit-record conversion helpers.

use prost::Message;
use sha2::{Digest, Sha256};

use crate::audit::{AuditEvent, ControlAudit, DecisionAudit};
use crate::controls::Evaluation;
use crate::pb::{
    AegisDecision, ControlResult, DecisionStatus, ReasonCode, SignalStatus, TradeSignal,
};

/// SHA-256 of the canonical request payload (deterministic protobuf encoding
/// of the decoded message).
pub(super) fn payload_hash(signal: &TradeSignal) -> [u8; 32] {
    Sha256::digest(signal.encode_to_vec()).into()
}

pub(super) fn base(signal_id: &str, now_ns: i64, state_seq: u64) -> AegisDecision {
    AegisDecision {
        signal_id: signal_id.to_owned(),
        decided_at_ns: now_ns,
        aegis_state_seq: state_seq,
        aegis_version: crate::VERSION.to_owned(),
        ..AegisDecision::default()
    }
}

/// Copy the pipeline outcome into a decision.
pub(super) fn from_eval(mut d: AegisDecision, ev: &Evaluation) -> AegisDecision {
    d.decision = ev.decision as i32;
    d.signal_status = ev.signal_status as i32;
    d.results = ev.results.clone();
    d.reasons = ev.reasons.iter().map(|r| *r as i32).collect();
    d
}

/// A REJECTED decision carrying a single failed pseudo-control.
pub(super) fn rejected(
    signal_id: &str,
    now_ns: i64,
    state_seq: u64,
    reason: ReasonCode,
    id: &str,
    detail: &str,
) -> AegisDecision {
    let mut d = base(signal_id, now_ns, state_seq);
    downgrade(&mut d, reason, id, detail);
    d
}

/// Turn any decision into a REJECTED one: strips the order, attestation and
/// hold, and records why. Used whenever a post-decision step fails.
pub(super) fn downgrade(d: &mut AegisDecision, reason: ReasonCode, id: &str, detail: &str) {
    d.decision = DecisionStatus::DecisionRejected as i32;
    d.signal_status = SignalStatus::SignalRejectedHardBlock as i32;
    d.attestation = None;
    d.order = None;
    d.hold_id = String::new();
    d.hold_expires_at_ns = 0;
    d.results
        .push(ControlResult::fail(id, true, reason, "", "failed").with_detail(detail));
    if !d.reasons.contains(&(reason as i32)) {
        d.reasons.push(reason as i32);
    }
}

fn name<E: TryFrom<i32>>(raw: i32, f: fn(&E) -> &'static str) -> String {
    E::try_from(raw).map_or_else(|_| format!("UNKNOWN({raw})"), |e| f(&e).to_owned())
}

/// Audit record for a decision: identifiers, enum names and thresholds only.
pub(super) fn decision_audit(d: &AegisDecision, symbol: &str, limits_sha: &str) -> AuditEvent {
    AuditEvent::Decision(DecisionAudit {
        signal_id: d.signal_id.clone(),
        symbol: symbol.to_owned(),
        decision: name::<DecisionStatus>(d.decision, DecisionStatus::as_str_name),
        signal_status: name::<SignalStatus>(d.signal_status, SignalStatus::as_str_name),
        reasons: d
            .reasons
            .iter()
            .map(|r| name::<ReasonCode>(*r, ReasonCode::as_str_name))
            .collect(),
        controls: d
            .results
            .iter()
            .map(|r| ControlAudit {
                id: r.control_id.clone(),
                hard: r.is_hard,
                passed: r.passed,
                reason: name::<ReasonCode>(r.reason, ReasonCode::as_str_name),
                threshold: r.threshold.clone(),
                observed: r.observed.clone(),
            })
            .collect(),
        order_id: d
            .order
            .as_ref()
            .map(|o| o.order_id.clone())
            .unwrap_or_default(),
        attestation_key_id: d
            .attestation
            .as_ref()
            .map(|a| a.key_id.clone())
            .unwrap_or_default(),
        kill_state_seq: d.aegis_state_seq,
        limits_sha256: limits_sha.to_owned(),
        aegis_version: d.aegis_version.clone(),
        decided_at_ns: d.decided_at_ns,
    })
}
