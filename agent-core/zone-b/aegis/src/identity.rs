//! Caller identity and authorization (spec 4.6 / 6, operator authentication is
//! TODO(owner): this is the PROPOSED "mTLS client certificate per identity"
//! option, plus signed reset approvals).
//!
//! * Transport identity: the subject CN (else the first DNS/URI SAN) of the
//!   verified client certificate is looked up in `peers` to obtain RPC roles.
//!   A certificate that is not listed has NO roles (default deny).
//! * Reset approvals: a kill-switch reset carries `Authorization` entries. The
//!   `credential_ref` must be the hex Ed25519 signature, by the approver's
//!   registered key, over the canonical text
//!   `afe-reset-v1\ntrigger_id=..\napprover_id=..\nrole=..\napproved_at_ns=..\n`
//!   and the approval must be fresh (`approval_max_age_ms`). An approver id or
//!   role that is not in the registry, or a bad signature, refuses the reset.
//!   This proves each approver individually, not just the connection.
//! * Hold approvals (ALI-164, `afe-hold-v1`) are signed the same way by a
//!   registered approver, or (owner decision 2026-09-29, DECISIONS row 4) are
//!   OIDC-attested: `credential_ref = "oidc-attested:<attestor_id>:<hex sig>"`,
//!   signed by a `hold_attestors` key over the same `afe-hold-v1` text with
//!   `approver_id` = the approver's OIDC subject. Only the peer named by that
//!   attestor entry (hitl-backend's certificate CN) may present such approvals,
//!   and they are never accepted for kill-switch resets. This trusts the
//!   attestor (hitl-backend, which re-verifies the operator's token) and its
//!   IdP, not a key held by each person.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use ed25519_dalek::{Signature, Verifier as _, VerifyingKey};
use serde::Deserialize;

use crate::error::ConfigError;
use crate::hex;
use crate::killswitch::{ResetRefusal, Role, VerifiedApproval};
use crate::pb;

/// RPC roles a peer certificate may hold.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum PeerRole {
    SignalSubmitter,
    HoldResolver,
    KillTrigger,
    KillReset,
    Operator,
    StateReader,
    ExecutionReporter,
    /// May push reference data (`PushReferenceData`); nothing else.
    MarketDataWriter,
}

impl PeerRole {
    pub const ALL: [PeerRole; 8] = [
        PeerRole::SignalSubmitter,
        PeerRole::HoldResolver,
        PeerRole::KillTrigger,
        PeerRole::KillReset,
        PeerRole::Operator,
        PeerRole::StateReader,
        PeerRole::ExecutionReporter,
        PeerRole::MarketDataWriter,
    ];

    fn parse(s: &str) -> Option<PeerRole> {
        Some(match s {
            "signal-submitter" => PeerRole::SignalSubmitter,
            "hold-resolver" => PeerRole::HoldResolver,
            "kill-trigger" => PeerRole::KillTrigger,
            "kill-reset" => PeerRole::KillReset,
            "operator" => PeerRole::Operator,
            "state-reader" => PeerRole::StateReader,
            "execution-reporter" => PeerRole::ExecutionReporter,
            "market-data-writer" => PeerRole::MarketDataWriter,
            _ => return None,
        })
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ApproverFile {
    roles: Vec<String>,
    ed25519_pubkey_hex: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AttestorFile {
    /// Certificate CN of the only peer that may present this attestor's approvals.
    peer: String,
    roles: Vec<String>,
    ed25519_pubkey_hex: String,
    /// The IdP whose subjects this attestor vouches for (recorded, not contacted).
    idp_issuer: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct IdentityFile {
    /// Certificate subject CN -> RPC roles.
    peers: BTreeMap<String, Vec<String>>,
    /// Approver id -> allowed approval roles and public key.
    approvers: BTreeMap<String, ApproverFile>,
    /// Attestor id -> key that vouches for OIDC-authenticated hold approvers (optional).
    #[serde(default)]
    hold_attestors: BTreeMap<String, AttestorFile>,
}

struct Approver {
    roles: BTreeSet<Role>,
    key: VerifyingKey,
}

struct Attestor {
    peer: String,
    roles: BTreeSet<Role>,
    key: VerifyingKey,
}

/// Validated identity registry.
pub struct Identities {
    peers: BTreeMap<String, BTreeSet<PeerRole>>,
    approvers: BTreeMap<String, Approver>,
    attestors: BTreeMap<String, Attestor>,
}

/// `credential_ref` prefix of an OIDC-attested hold approval.
pub const ATTESTED_PREFIX: &str = "oidc-attested:";
/// Longest OIDC subject accepted as an attested approver id.
const MAX_ATTESTED_SUBJECT: usize = 128;

/// A verified hold approval: who, in which role, and through which attestor
/// (`None` for a registered approver's own key).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VerifiedHoldApproval {
    pub approver_id: String,
    pub role: Role,
    pub attestor: Option<String>,
}

/// Why a hold release's approvals were refused. `Precondition` maps to
/// FAILED_PRECONDITION (the request is incomplete or inconsistent), `Denied`
/// to PERMISSION_DENIED (an approval does not authenticate).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HoldApprovalRefusal {
    Precondition(String),
    Denied(String),
}

impl std::fmt::Debug for Identities {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Identities")
            .field("peers", &self.peers.keys().collect::<Vec<_>>())
            .field("approvers", &self.approvers.keys().collect::<Vec<_>>())
            .field("hold_attestors", &self.attestors.keys().collect::<Vec<_>>())
            .finish()
    }
}

fn invalid(msg: String) -> ConfigError {
    ConfigError::Invalid(msg)
}

impl Identities {
    pub fn from_path(path: &Path) -> Result<Identities, ConfigError> {
        let bytes = std::fs::read(path).map_err(|e| ConfigError::Unreadable {
            what: "identities file",
            detail: e.to_string(),
        })?;
        Identities::from_bytes(&bytes)
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Identities, ConfigError> {
        let file: IdentityFile =
            serde_json::from_slice(bytes).map_err(|e| invalid(e.to_string()))?;
        let mut peers = BTreeMap::new();
        for (cn, roles) in file.peers {
            let parsed: Option<BTreeSet<PeerRole>> =
                roles.iter().map(|r| PeerRole::parse(r)).collect();
            let roles =
                parsed.ok_or_else(|| invalid(format!("peer {cn:?} has an unknown role")))?;
            peers.insert(cn, roles);
        }
        let mut approvers = BTreeMap::new();
        for (id, a) in file.approvers {
            let roles: Option<BTreeSet<Role>> = a.roles.iter().map(|r| Role::parse(r)).collect();
            let roles = roles
                .filter(|r| !r.is_empty())
                .ok_or_else(|| invalid(format!("approver {id:?} needs known roles")))?;
            let key = parse_key(&a.ed25519_pubkey_hex, "approver", &id)?;
            approvers.insert(id, Approver { roles, key });
        }
        let mut attestors = BTreeMap::new();
        for (id, a) in file.hold_attestors {
            if id.is_empty() || id.contains(':') || id.chars().any(char::is_control) {
                return Err(invalid(format!(
                    "hold attestor id {id:?} must be non-empty, without ':'"
                )));
            }
            if a.peer.trim().is_empty() || a.idp_issuer.trim().is_empty() {
                return Err(invalid(format!(
                    "hold attestor {id:?} needs a peer and an idp_issuer"
                )));
            }
            let roles: Option<BTreeSet<Role>> = a.roles.iter().map(|r| Role::parse(r)).collect();
            let roles = roles
                .filter(|r| !r.is_empty())
                .ok_or_else(|| invalid(format!("hold attestor {id:?} needs known roles")))?;
            let key = parse_key(&a.ed25519_pubkey_hex, "hold attestor", &id)?;
            attestors.insert(
                id,
                Attestor {
                    peer: a.peer,
                    roles,
                    key,
                },
            );
        }
        Ok(Identities {
            peers,
            approvers,
            attestors,
        })
    }

    /// Roles of a certificate identity; unknown identities have none.
    pub fn roles_of(&self, cn: &str) -> BTreeSet<PeerRole> {
        self.peers.get(cn).cloned().unwrap_or_default()
    }

    /// Verify one signed approval for `trigger_id`.
    pub fn verify_approval(
        &self,
        trigger_id: &str,
        a: &pb::Authorization,
        now_ns: i64,
        max_age_ms: u64,
    ) -> Result<VerifiedApproval, ResetRefusal> {
        let text = approval_text(trigger_id, &a.approver_id, &a.role, a.approved_at_ns);
        self.verify_signed(&text, a, now_ns, max_age_ms)
    }

    /// Verify a signed second approval for releasing (or rejecting) `hold_id` (ALI-164).
    /// The decision is part of the signed text, so an approval to reject cannot be
    /// replayed to release.
    pub fn verify_hold_approval(
        &self,
        hold_id: &str,
        approve: bool,
        a: &pb::Authorization,
        now_ns: i64,
        max_age_ms: u64,
    ) -> Result<VerifiedApproval, ResetRefusal> {
        let text = hold_approval_text(hold_id, approve, &a.approver_id, &a.role, a.approved_at_ns);
        self.verify_signed(&text, a, now_ns, max_age_ms)
    }

    fn verify_signed(
        &self,
        text: &str,
        a: &pb::Authorization,
        now_ns: i64,
        max_age_ms: u64,
    ) -> Result<VerifiedApproval, ResetRefusal> {
        let deny = |why: &str| {
            ResetRefusal::Unauthenticated(format!("approval by {:?}: {why}", a.approver_id))
        };
        let approver = self
            .approvers
            .get(&a.approver_id)
            .ok_or_else(|| deny("unknown approver"))?;
        let role = check_signature(
            &approver.key,
            &approver.roles,
            text,
            a,
            &a.credential_ref,
            now_ns,
            max_age_ms,
        )
        .map_err(deny)?;
        Ok(VerifiedApproval {
            approver_id: a.approver_id.clone(),
            role,
        })
    }

    /// Verify one hold approval presented by the peer `caller`: a registered
    /// approver's own signature, or an OIDC attestation by a `hold_attestors`
    /// key that names `caller` as its peer.
    pub fn verify_hold_authorization(
        &self,
        caller: &str,
        hold_id: &str,
        approve: bool,
        a: &pb::Authorization,
        now_ns: i64,
        max_age_ms: u64,
    ) -> Result<VerifiedHoldApproval, ResetRefusal> {
        let Some(rest) = a.credential_ref.strip_prefix(ATTESTED_PREFIX) else {
            let v = self.verify_hold_approval(hold_id, approve, a, now_ns, max_age_ms)?;
            return Ok(VerifiedHoldApproval {
                approver_id: v.approver_id,
                role: v.role,
                attestor: None,
            });
        };
        let deny = |why: &str| {
            ResetRefusal::Unauthenticated(format!(
                "attested approval of {:?}: {why}",
                a.approver_id
            ))
        };
        let (attestor_id, sig_hex) = rest
            .split_once(':')
            .ok_or_else(|| deny("malformed credential"))?;
        let attestor = self
            .attestors
            .get(attestor_id)
            .ok_or_else(|| deny("unknown attestor"))?;
        if attestor.peer != caller {
            return Err(deny("attestor is not bound to this peer"));
        }
        let subject = &a.approver_id;
        if subject.trim().is_empty()
            || subject.len() > MAX_ATTESTED_SUBJECT
            || subject.chars().any(char::is_control)
        {
            return Err(deny("unusable subject"));
        }
        let text = hold_approval_text(hold_id, approve, subject, &a.role, a.approved_at_ns);
        let role = check_signature(
            &attestor.key,
            &attestor.roles,
            &text,
            a,
            sig_hex,
            now_ns,
            max_age_ms,
        )
        .map_err(deny)?;
        Ok(VerifiedHoldApproval {
            approver_id: subject.clone(),
            role,
            attestor: Some(attestor_id.to_owned()),
        })
    }

    /// All approval checks for a `ResolveHold` (ALI-164 + owner decision
    /// 2026-09-29). A REJECT needs none. A RELEASE needs `second_approval` when
    /// `required`; any approval that is present is verified (fail closed). The
    /// second approval must be by `second_approver_id` and not the operator; an
    /// attested second approval also needs the first approver's approval, and the
    /// two must be different people. Both must carry the operator role.
    pub fn verify_hold_release(
        &self,
        caller: &str,
        req: &pb::ResolveHoldRequest,
        required: bool,
        now_ns: i64,
        max_age_ms: u64,
    ) -> Result<(), HoldApprovalRefusal> {
        use HoldApprovalRefusal::{Denied, Precondition};
        if !req.approve {
            return Ok(());
        }
        let Some(second) = req.second_approval.as_ref() else {
            if required {
                return Err(Precondition(
                    "a signed second approval is required to release a hold".into(),
                ));
            }
            if req.first_approval.is_some() {
                return Err(Precondition(
                    "a first approval was sent without a second approval".into(),
                ));
            }
            return Ok(());
        };
        if second.approver_id != req.second_approver_id || second.approver_id == req.operator_id {
            return Err(Precondition(
                "the second approval must be signed by second_approver_id, not the operator".into(),
            ));
        }
        let verify = |a: &pb::Authorization| {
            let v = self
                .verify_hold_authorization(caller, &req.hold_id, req.approve, a, now_ns, max_age_ms)
                .map_err(|e| Denied(e.to_string()))?;
            if v.role != Role::Operator {
                return Err(Denied("hold approvals must carry the operator role".into()));
            }
            Ok(v)
        };
        let s = verify(second)?;
        match req.first_approval.as_ref() {
            None if s.attestor.is_some() => Err(Precondition(
                "an attested second approval needs the attested first approval".into(),
            )),
            None => Ok(()),
            Some(first) => {
                let f = verify(first)?;
                if f.approver_id == s.approver_id {
                    return Err(Precondition(
                        "the same approver cannot approve a hold twice".into(),
                    ));
                }
                Ok(())
            }
        }
    }
}

fn parse_key(hex_key: &str, what: &str, id: &str) -> Result<VerifyingKey, ConfigError> {
    let raw = hex::decode(hex_key)
        .and_then(|b| <[u8; 32]>::try_from(b).ok())
        .ok_or_else(|| invalid(format!("{what} {id:?}: bad ed25519_pubkey_hex")))?;
    VerifyingKey::from_bytes(&raw)
        .map_err(|_| invalid(format!("{what} {id:?}: invalid public key")))
}

/// Role, freshness and signature checks shared by every signed approval.
fn check_signature(
    key: &VerifyingKey,
    roles: &BTreeSet<Role>,
    text: &str,
    a: &pb::Authorization,
    sig_hex: &str,
    now_ns: i64,
    max_age_ms: u64,
) -> Result<Role, &'static str> {
    let role = Role::parse(&a.role).ok_or("unknown role")?;
    if !roles.contains(&role) {
        return Err("role not held by approver");
    }
    let max_age_ns = i64::try_from(max_age_ms)
        .unwrap_or(i64::MAX / 1_000_000)
        .saturating_mul(1_000_000);
    let age = now_ns.saturating_sub(a.approved_at_ns);
    if age > max_age_ns || age < -max_age_ns / 100 {
        return Err("approval is stale or from the future");
    }
    let sig = hex::decode(sig_hex)
        .and_then(|b| Signature::from_slice(&b).ok())
        .ok_or("credential is not a signature")?;
    key.verify(text.as_bytes(), &sig)
        .map_err(|_| "bad signature")?;
    Ok(role)
}

/// Canonical text an approver signs to authorize a reset.
pub fn approval_text(
    trigger_id: &str,
    approver_id: &str,
    role: &str,
    approved_at_ns: i64,
) -> String {
    format!("afe-reset-v1\ntrigger_id={trigger_id}\napprover_id={approver_id}\nrole={role}\napproved_at_ns={approved_at_ns}\n")
}

/// Canonical text a second approver signs for a hold decision (ALI-164). Binds the
/// hold and the decision, so it cannot be replayed onto another hold or flipped.
pub fn hold_approval_text(
    hold_id: &str,
    approve: bool,
    approver_id: &str,
    role: &str,
    approved_at_ns: i64,
) -> String {
    format!("afe-hold-v1\nhold_id={hold_id}\napprove={approve}\napprover_id={approver_id}\nrole={role}\napproved_at_ns={approved_at_ns}\n")
}

/// Subject CN of a DER certificate, else the first DNS/URI SAN.
pub fn cert_identity(der: &[u8]) -> Option<String> {
    use x509_parser::extensions::GeneralName;
    use x509_parser::prelude::{FromDer, X509Certificate};
    let (_, cert) = X509Certificate::from_der(der).ok()?;
    if let Some(cn) = cert
        .subject()
        .iter_common_name()
        .next()
        .and_then(|c| c.as_str().ok())
    {
        return Some(cn.to_owned());
    }
    let san = cert.subject_alternative_name().ok().flatten()?;
    san.value.general_names.iter().find_map(|n| match n {
        GeneralName::DNSName(d) => Some((*d).to_owned()),
        GeneralName::URI(u) => Some((*u).to_owned()),
        _ => None,
    })
}

#[cfg(test)]
pub(crate) mod tests;
