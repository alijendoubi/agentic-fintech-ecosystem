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
struct IdentityFile {
    /// Certificate subject CN -> RPC roles.
    peers: BTreeMap<String, Vec<String>>,
    /// Approver id -> allowed approval roles and public key.
    approvers: BTreeMap<String, ApproverFile>,
}

struct Approver {
    roles: BTreeSet<Role>,
    key: VerifyingKey,
}

/// Validated identity registry.
pub struct Identities {
    peers: BTreeMap<String, BTreeSet<PeerRole>>,
    approvers: BTreeMap<String, Approver>,
}

impl std::fmt::Debug for Identities {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Identities")
            .field("peers", &self.peers.keys().collect::<Vec<_>>())
            .field("approvers", &self.approvers.keys().collect::<Vec<_>>())
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
            let raw = hex::decode(&a.ed25519_pubkey_hex)
                .and_then(|b| <[u8; 32]>::try_from(b).ok())
                .ok_or_else(|| invalid(format!("approver {id:?}: bad ed25519_pubkey_hex")))?;
            let key = VerifyingKey::from_bytes(&raw)
                .map_err(|_| invalid(format!("approver {id:?}: invalid public key")))?;
            approvers.insert(id, Approver { roles, key });
        }
        Ok(Identities { peers, approvers })
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
        let role = Role::parse(&a.role).ok_or_else(|| deny("unknown role"))?;
        if !approver.roles.contains(&role) {
            return Err(deny("role not held by approver"));
        }
        let max_age_ns = i64::try_from(max_age_ms)
            .unwrap_or(i64::MAX / 1_000_000)
            .saturating_mul(1_000_000);
        let age = now_ns.saturating_sub(a.approved_at_ns);
        if age > max_age_ns || age < -max_age_ns / 100 {
            return Err(deny("approval is stale or from the future"));
        }
        let sig = hex::decode(&a.credential_ref)
            .and_then(|b| Signature::from_slice(&b).ok())
            .ok_or_else(|| deny("credential is not a signature"))?;
        approver
            .key
            .verify(text.as_bytes(), &sig)
            .map_err(|_| deny("bad signature"))?;
        Ok(VerifiedApproval {
            approver_id: a.approver_id.clone(),
            role,
        })
    }
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
