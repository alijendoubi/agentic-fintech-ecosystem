//! Who may reset which latch (spec 5.2 "Reset authority" column, existing
//! wording from the RTS6 template):
//!
//! | Latch level | Requirement (this implementation)                                  |
//! |-------------|--------------------------------------------------------------------|
//! | SOFT        | 1 operator                                                          |
//! | LOGIC       | 2 distinct operators + root-cause reference                         |
//! | DEAD_MANS   | 1 operator + an operator heartbeat received after the latch         |
//! | HARD        | 2 distinct operators + 1 compliance officer + root-cause reference  |
//! | PHYSICAL    | as HARD; the root-cause reference must be the incident report       |
//!
//! "Dual operator + compliance sign-off" (HARD) is ambiguous in the source
//! text; the stricter reading (three distinct people) is implemented.
//! TODO(owner): confirm. Approvals reaching this module are ALREADY
//! authenticated (see `identity`): an approver id here has been proven by a
//! signature from that approver's registered key and its role checked.

use std::collections::BTreeSet;

use thiserror::Error;

use crate::pb::KillSwitchLevel;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Role {
    Operator,
    Compliance,
}

impl Role {
    pub fn parse(s: &str) -> Option<Role> {
        match s {
            "operator" => Some(Role::Operator),
            "compliance" => Some(Role::Compliance),
            _ => None,
        }
    }
}

/// An approval whose signature and role have already been verified.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedApproval {
    pub approver_id: String,
    pub role: Role,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResetRequest {
    pub trigger_id: String,
    pub approvals: Vec<VerifiedApproval>,
    pub root_cause_ref: String,
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum ResetRefusal {
    #[error("unknown trigger_id")]
    UnknownTrigger,
    #[error("insufficient approvals: need {operators} operator(s) and {compliance} compliance approver(s), distinct people")]
    InsufficientApprovals { operators: usize, compliance: usize },
    #[error("a root-cause reference is required for this level")]
    RootCauseRequired,
    #[error("an operator heartbeat after the latch is required to reset the dead-man's latch")]
    HeartbeatRequired,
    #[error("approvals could not be authenticated: {0}")]
    Unauthenticated(String),
    #[error("state store unavailable, reset refused")]
    StoreUnavailable,
    #[error("audit sink unavailable, reset refused")]
    AuditUnavailable,
}

struct Requirement {
    operators: usize,
    compliance: usize,
    root_cause: bool,
    heartbeat: bool,
}

fn requirement(level: KillSwitchLevel) -> Requirement {
    use KillSwitchLevel::*;
    let (operators, compliance, root_cause, heartbeat) = match level {
        KillLevelSoft => (1, 0, false, false),
        KillLevelLogic => (2, 0, true, false),
        KillLevelDeadMans => (1, 0, false, true),
        // HARD, PHYSICAL and anything unrecognised: the strictest rule.
        KillLevelHard | KillLevelPhysical | KillLevelNormal => (2, 1, true, false),
    };
    Requirement {
        operators,
        compliance,
        root_cause,
        heartbeat,
    }
}

/// Count distinct people per role; one person counts once, in the first role
/// they were seen with, so nobody can fill two seats.
fn seats(approvals: &[VerifiedApproval]) -> (usize, usize) {
    let mut seen = BTreeSet::new();
    let (mut ops, mut comp) = (0, 0);
    for a in approvals {
        if !seen.insert(a.approver_id.as_str()) {
            continue;
        }
        match a.role {
            Role::Operator => ops += 1,
            Role::Compliance => comp += 1,
        }
    }
    (ops, comp)
}

/// Check the reset authority for a latch at `level`.
pub fn authorize(
    level: KillSwitchLevel,
    latched_at_ns: i64,
    last_heartbeat_ns: i64,
    req: &ResetRequest,
) -> Result<(), ResetRefusal> {
    let need = requirement(level);
    let (ops, comp) = seats(&req.approvals);
    if ops < need.operators || comp < need.compliance {
        return Err(ResetRefusal::InsufficientApprovals {
            operators: need.operators,
            compliance: need.compliance,
        });
    }
    if need.root_cause && req.root_cause_ref.trim().is_empty() {
        return Err(ResetRefusal::RootCauseRequired);
    }
    if need.heartbeat && last_heartbeat_ns <= latched_at_ns {
        return Err(ResetRefusal::HeartbeatRequired);
    }
    Ok(())
}
