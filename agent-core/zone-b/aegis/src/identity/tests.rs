use ed25519_dalek::{Signer as _, SigningKey};

use super::*;
use crate::hex;

pub(crate) const NOW: i64 = 1_790_000_000_000_000_000;
const MAX_AGE_MS: u64 = 300_000;

pub(crate) fn key(seed: u8) -> SigningKey {
    SigningKey::from_bytes(&[seed; 32])
}

pub(crate) fn registry_json() -> String {
    let pk = |s: u8| hex::encode(key(s).verifying_key().as_bytes());
    format!(
        r#"{{"peers": {{"cognitive-core": ["signal-submitter"], "operator-console": ["operator","kill-trigger","kill-reset","hold-resolver","state-reader"],
                       "execution-motor": ["state-reader","execution-reporter"], "monitor": ["kill-trigger"]}},
            "approvers": {{"alice": {{"roles": ["operator"], "ed25519_pubkey_hex": "{}"}},
                           "bob": {{"roles": ["operator"], "ed25519_pubkey_hex": "{}"}},
                           "carol": {{"roles": ["compliance","operator"], "ed25519_pubkey_hex": "{}"}}}}}}"#,
        pk(1),
        pk(2),
        pk(3)
    )
}

pub(crate) fn signed(
    seed: u8,
    trigger: &str,
    approver: &str,
    role: &str,
    at: i64,
) -> pb::Authorization {
    let text = approval_text(trigger, approver, role, at);
    pb::Authorization {
        approver_id: approver.into(),
        role: role.into(),
        approved_at_ns: at,
        credential_ref: hex::encode(&key(seed).sign(text.as_bytes()).to_bytes()),
    }
}

fn ids() -> Identities {
    Identities::from_bytes(registry_json().as_bytes()).unwrap()
}

#[test]
fn peers_get_only_their_roles_and_unknown_certs_get_none() {
    let i = ids();
    assert!(i
        .roles_of("cognitive-core")
        .contains(&PeerRole::SignalSubmitter));
    assert!(!i.roles_of("cognitive-core").contains(&PeerRole::KillReset));
    assert!(i.roles_of("stranger").is_empty());
    assert!(i.roles_of("").is_empty());
}

#[test]
fn a_valid_signed_approval_verifies() {
    let a = signed(1, "t-1", "alice", "operator", NOW - 1_000_000_000);
    let v = ids().verify_approval("t-1", &a, NOW, MAX_AGE_MS).unwrap();
    assert_eq!(
        v,
        VerifiedApproval {
            approver_id: "alice".into(),
            role: Role::Operator
        }
    );
}

#[test]
fn approvals_are_bound_to_trigger_approver_role_and_time() {
    let i = ids();
    let good = signed(1, "t-1", "alice", "operator", NOW);
    // wrong trigger (replay of an approval for another latch)
    assert!(i.verify_approval("t-2", &good, NOW, MAX_AGE_MS).is_err());
    // claiming to be somebody else with alice's signature
    let mut forged = good.clone();
    forged.approver_id = "bob".into();
    assert!(i.verify_approval("t-1", &forged, NOW, MAX_AGE_MS).is_err());
    // signed by the wrong key
    let wrong_key = signed(2, "t-1", "alice", "operator", NOW);
    assert!(i
        .verify_approval("t-1", &wrong_key, NOW, MAX_AGE_MS)
        .is_err());
    // role not held: alice is only an operator
    let role = signed(1, "t-1", "alice", "compliance", NOW);
    assert!(i.verify_approval("t-1", &role, NOW, MAX_AGE_MS).is_err());
    // tampered timestamp
    let mut moved = good.clone();
    moved.approved_at_ns += 1;
    assert!(i
        .verify_approval("t-1", &moved, NOW + 1, MAX_AGE_MS)
        .is_err());
}

#[test]
fn stale_future_unknown_and_malformed_approvals_are_refused() {
    let i = ids();
    let stale = signed(1, "t", "alice", "operator", NOW - 301_000_000_000);
    assert!(i.verify_approval("t", &stale, NOW, MAX_AGE_MS).is_err());
    let edge = signed(1, "t", "alice", "operator", NOW - 300_000_000_000);
    assert!(i.verify_approval("t", &edge, NOW, MAX_AGE_MS).is_ok());
    let future = signed(1, "t", "alice", "operator", NOW + 60_000_000_000);
    assert!(i.verify_approval("t", &future, NOW, MAX_AGE_MS).is_err());
    let unknown = signed(9, "t", "mallory", "operator", NOW);
    assert!(i.verify_approval("t", &unknown, NOW, MAX_AGE_MS).is_err());
    for cred in ["", "zz", "abcd", &"00".repeat(64)] {
        let mut a = signed(1, "t", "alice", "operator", NOW);
        a.credential_ref = cred.into();
        assert!(
            i.verify_approval("t", &a, NOW, MAX_AGE_MS).is_err(),
            "{cred:?}"
        );
    }
}

#[test]
fn registry_validation_rejects_bad_files() {
    for bad in [
        "{}",
        "not json",
        r#"{"peers": {"x": ["god-mode"]}, "approvers": {}}"#,
        r#"{"peers": {}, "approvers": {"a": {"roles": [], "ed25519_pubkey_hex": "00"}}}"#,
        r#"{"peers": {}, "approvers": {"a": {"roles": ["operator"], "ed25519_pubkey_hex": "zz"}}}"#,
        r#"{"peers": {}, "approvers": {}, "extra": 1}"#,
    ] {
        assert!(Identities::from_bytes(bad.as_bytes()).is_err(), "{bad}");
    }
    assert!(Identities::from_bytes(br#"{"peers": {}, "approvers": {}}"#).is_ok());
}

#[test]
fn cert_identity_reads_cn_then_san() {
    let mut p = rcgen::CertificateParams::new(vec!["san.example".to_owned()]).unwrap();
    p.distinguished_name
        .push(rcgen::DnType::CommonName, "operator-console");
    let kp = rcgen::KeyPair::generate().unwrap();
    let cert = p.self_signed(&kp).unwrap();
    assert_eq!(
        cert_identity(cert.der()).as_deref(),
        Some("operator-console")
    );
    let p2 = rcgen::CertificateParams::new(vec!["only-san.example".to_owned()]).unwrap();
    let cert2 = p2
        .self_signed(&rcgen::KeyPair::generate().unwrap())
        .unwrap();
    let id = cert_identity(cert2.der());
    assert!(
        id.is_some(),
        "falls back to a SAN when there is no CN: {id:?}"
    );
    assert_eq!(cert_identity(b"not a cert"), None);
}

// ---- ALI-164: signed second approvals for hold decisions ----

fn hold_signed(seed: u8, hold: &str, approve: bool, approver: &str, at: i64) -> pb::Authorization {
    let text = hold_approval_text(hold, approve, approver, "operator", at);
    pb::Authorization {
        approver_id: approver.into(),
        role: "operator".into(),
        approved_at_ns: at,
        credential_ref: hex::encode(&key(seed).sign(text.as_bytes()).to_bytes()),
    }
}

#[test]
fn a_signed_hold_approval_verifies_for_its_hold_and_decision() {
    let a = hold_signed(2, "hold-1", true, "bob", NOW - 1_000_000_000);
    let v = ids()
        .verify_hold_approval("hold-1", true, &a, NOW, MAX_AGE_MS)
        .unwrap();
    assert_eq!(v.approver_id, "bob");
    assert_eq!(v.role, Role::Operator);
}

#[test]
fn a_hold_approval_cannot_be_replayed_onto_another_hold_or_decision() {
    let a = hold_signed(2, "hold-1", false, "bob", NOW - 1_000_000_000);
    let i = ids();
    assert!(
        i.verify_hold_approval("hold-1", true, &a, NOW, MAX_AGE_MS)
            .is_err(),
        "an approval to reject must not release"
    );
    assert!(i
        .verify_hold_approval("hold-2", false, &a, NOW, MAX_AGE_MS)
        .is_err());
    let reset_style = signed(2, "hold-1", "bob", "operator", NOW - 1_000_000_000);
    assert!(
        i.verify_hold_approval("hold-1", true, &reset_style, NOW, MAX_AGE_MS)
            .is_err(),
        "a kill-switch reset signature must not count as a hold approval"
    );
}

#[test]
fn a_hold_approval_signed_by_the_wrong_key_or_stale_is_refused() {
    let i = ids();
    let forged = hold_signed(9, "hold-1", true, "bob", NOW - 1_000_000_000);
    assert!(i
        .verify_hold_approval("hold-1", true, &forged, NOW, MAX_AGE_MS)
        .is_err());
    let stale = hold_signed(2, "hold-1", true, "bob", NOW - 301_000_000_000);
    assert!(i
        .verify_hold_approval("hold-1", true, &stale, NOW, MAX_AGE_MS)
        .is_err());
}
