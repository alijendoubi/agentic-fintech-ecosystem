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

// ---- Owner decision 2026-09-29 (DECISIONS row 4): OIDC-attested hold approvals ----

const ATTESTOR_SEED: u8 = 7;
const HITL: &str = "hitl-backend";

fn attested_ids() -> Identities {
    let pk = |s: u8| hex::encode(key(s).verifying_key().as_bytes());
    let json = format!(
        r#"{{"peers": {{"hitl-backend": ["hold-resolver"], "operator-console": ["hold-resolver"]}},
            "approvers": {{"bob": {{"roles": ["operator"], "ed25519_pubkey_hex": "{}"}}}},
            "hold_attestors": {{"hitl-oidc": {{"peer": "hitl-backend", "roles": ["operator"],
                                "ed25519_pubkey_hex": "{}", "idp_issuer": "https://idp.test"}}}}}}"#,
        pk(2),
        pk(ATTESTOR_SEED)
    );
    Identities::from_bytes(json.as_bytes()).unwrap()
}

fn attested(seed: u8, attestor: &str, hold: &str, approve: bool, sub: &str) -> pb::Authorization {
    let at = NOW - 1_000_000_000;
    let text = hold_approval_text(hold, approve, sub, "operator", at);
    let sig = hex::encode(&key(seed).sign(text.as_bytes()).to_bytes());
    pb::Authorization {
        approver_id: sub.into(),
        role: "operator".into(),
        approved_at_ns: at,
        credential_ref: format!("{ATTESTED_PREFIX}{attestor}:{sig}"),
    }
}

fn release(
    first: Option<pb::Authorization>,
    second: Option<pb::Authorization>,
) -> pb::ResolveHoldRequest {
    pb::ResolveHoldRequest {
        hold_id: "hold-1".into(),
        operator_id: HITL.into(),
        second_approver_id: second
            .as_ref()
            .map(|a| a.approver_id.clone())
            .unwrap_or_default(),
        approve: true,
        second_approval: second,
        first_approval: first,
        ..Default::default()
    }
}

#[test]
fn an_attested_hold_approval_verifies_only_for_its_bound_peer() {
    let i = attested_ids();
    let a = attested(ATTESTOR_SEED, "hitl-oidc", "hold-1", true, "oidc|alice");
    let v = i
        .verify_hold_authorization(HITL, "hold-1", true, &a, NOW, MAX_AGE_MS)
        .unwrap();
    assert_eq!(v.approver_id, "oidc|alice");
    assert_eq!(v.role, Role::Operator);
    assert_eq!(v.attestor.as_deref(), Some("hitl-oidc"));
    assert!(
        i.verify_hold_authorization("operator-console", "hold-1", true, &a, NOW, MAX_AGE_MS)
            .is_err(),
        "another hold-resolver peer must not present the attestor's approvals"
    );
}

#[test]
fn attested_approvals_are_refused_when_forged_stale_replayed_or_malformed() {
    let i = attested_ids();
    let check = |a: &pb::Authorization, hold: &str, approve: bool| {
        i.verify_hold_authorization(HITL, hold, approve, a, NOW, MAX_AGE_MS)
    };
    let good = attested(ATTESTOR_SEED, "hitl-oidc", "hold-1", true, "oidc|alice");
    assert!(check(&good, "hold-2", true).is_err(), "other hold");
    assert!(check(&good, "hold-1", false).is_err(), "flipped decision");
    let forged = attested(9, "hitl-oidc", "hold-1", true, "oidc|alice");
    assert!(check(&forged, "hold-1", true).is_err(), "wrong key");
    let unknown = attested(ATTESTOR_SEED, "other-idp", "hold-1", true, "oidc|alice");
    assert!(check(&unknown, "hold-1", true).is_err(), "unknown attestor");
    let mut stale = good.clone();
    stale.approved_at_ns = NOW - 301_000_000_000;
    assert!(check(&stale, "hold-1", true).is_err(), "stale");
    let mut renamed = good.clone();
    renamed.approver_id = "oidc|mallory".into();
    assert!(
        check(&renamed, "hold-1", true).is_err(),
        "subject not signed"
    );
    let mut no_sep = good.clone();
    no_sep.credential_ref = format!("{ATTESTED_PREFIX}hitl-oidc");
    assert!(
        check(&no_sep, "hold-1", true).is_err(),
        "malformed credential"
    );
    let injected = attested(
        ATTESTOR_SEED,
        "hitl-oidc",
        "hold-1",
        true,
        "a\nrole=operator",
    );
    assert!(
        check(&injected, "hold-1", true).is_err(),
        "control characters in the subject"
    );
    let mut compliance = good.clone();
    compliance.role = "compliance".into();
    assert!(
        check(&compliance, "hold-1", true).is_err(),
        "role not held by the attestor"
    );
}

#[test]
fn an_attested_approval_never_counts_for_a_kill_switch_reset() {
    let i = attested_ids();
    let a = attested(ATTESTOR_SEED, "hitl-oidc", "t-1", true, "bob");
    assert!(i.verify_approval("t-1", &a, NOW, MAX_AGE_MS).is_err());
}

#[test]
fn a_release_with_two_distinct_attested_subjects_passes() {
    let i = attested_ids();
    let first = attested(ATTESTOR_SEED, "hitl-oidc", "hold-1", true, "oidc|alice");
    let second = attested(ATTESTOR_SEED, "hitl-oidc", "hold-1", true, "oidc|bob");
    let req = release(Some(first), Some(second));
    assert_eq!(
        i.verify_hold_release(HITL, &req, true, NOW, MAX_AGE_MS),
        Ok(())
    );
    assert_eq!(
        i.verify_hold_release(HITL, &req, false, NOW, MAX_AGE_MS),
        Ok(())
    );
}

#[test]
fn a_release_refuses_the_same_subject_twice_and_missing_approvals() {
    use HoldApprovalRefusal::{Denied, Precondition};
    let i = attested_ids();
    let alice = || attested(ATTESTOR_SEED, "hitl-oidc", "hold-1", true, "oidc|alice");
    let bob = || attested(ATTESTOR_SEED, "hitl-oidc", "hold-1", true, "oidc|bob");
    let run =
        |req: &pb::ResolveHoldRequest| i.verify_hold_release(HITL, req, true, NOW, MAX_AGE_MS);

    assert!(matches!(
        run(&release(Some(alice()), Some(alice()))),
        Err(Precondition(_))
    ));
    assert!(matches!(
        run(&release(None, Some(bob()))),
        Err(Precondition(_))
    ));
    assert!(matches!(run(&release(None, None)), Err(Precondition(_))));
    assert!(matches!(
        run(&release(Some(alice()), None)),
        Err(Precondition(_))
    ));

    let mut named_other = release(Some(alice()), Some(bob()));
    named_other.second_approver_id = "oidc|carol".into();
    assert!(matches!(run(&named_other), Err(Precondition(_))));

    let other_hold = attested(ATTESTOR_SEED, "hitl-oidc", "hold-9", true, "oidc|alice");
    assert!(matches!(
        run(&release(Some(other_hold), Some(bob()))),
        Err(Denied(_))
    ));
    let forged_first = attested(9, "hitl-oidc", "hold-1", true, "oidc|alice");
    assert!(matches!(
        run(&release(Some(forged_first), Some(bob()))),
        Err(Denied(_))
    ));

    let mut reject = release(None, None);
    reject.approve = false;
    assert_eq!(run(&reject), Ok(()), "a rejection needs no approvals");
}

#[test]
fn a_registered_approver_still_releases_without_an_attested_first_approval() {
    let i = attested_ids();
    let bob = hold_signed(2, "hold-1", true, "bob", NOW - 1_000_000_000);
    let mut req = release(None, Some(bob));
    req.operator_id = "operator-console".into();
    assert_eq!(
        i.verify_hold_release("operator-console", &req, true, NOW, MAX_AGE_MS),
        Ok(())
    );
}

#[test]
fn hold_attestor_entries_are_validated() {
    let pk = hex::encode(key(ATTESTOR_SEED).verifying_key().as_bytes());
    let file = |attestor: &str| {
        format!(r#"{{"peers": {{}}, "approvers": {{}}, "hold_attestors": {{{attestor}}}}}"#)
    };
    let ok = file(&format!(
        r#""a": {{"peer": "hitl-backend", "roles": ["operator"], "ed25519_pubkey_hex": "{pk}", "idp_issuer": "https://idp"}}"#
    ));
    assert!(Identities::from_bytes(ok.as_bytes()).is_ok());
    for bad in [
        format!(r#""a:b": {{"peer": "p", "roles": ["operator"], "ed25519_pubkey_hex": "{pk}", "idp_issuer": "i"}}"#),
        format!(r#""a": {{"peer": "", "roles": ["operator"], "ed25519_pubkey_hex": "{pk}", "idp_issuer": "i"}}"#),
        format!(r#""a": {{"peer": "p", "roles": ["operator"], "ed25519_pubkey_hex": "{pk}", "idp_issuer": ""}}"#),
        format!(r#""a": {{"peer": "p", "roles": [], "ed25519_pubkey_hex": "{pk}", "idp_issuer": "i"}}"#),
        r#""a": {"peer": "p", "roles": ["operator"], "ed25519_pubkey_hex": "00", "idp_issuer": "i"}"#.to_owned(),
        format!(r#""a": {{"peer": "p", "roles": ["operator"], "ed25519_pubkey_hex": "{pk}", "idp_issuer": "i", "x": 1}}"#),
    ] {
        assert!(Identities::from_bytes(file(&bad).as_bytes()).is_err(), "{bad}");
    }
}

#[test]
fn an_attestation_made_by_hitl_backend_verifies() {
    // Test vector from hitl-backend (Python `ApprovalAttestor`, seed = bytes 0..32), so the two
    // implementations of the afe-hold-v1 text and the credential format cannot drift apart.
    let json = r#"{"peers": {"hitl-backend": ["hold-resolver"]}, "approvers": {},
        "hold_attestors": {"hitl-oidc": {"peer": "hitl-backend", "roles": ["operator"],
            "ed25519_pubkey_hex": "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",
            "idp_issuer": "https://idp.test"}}}"#;
    let i = Identities::from_bytes(json.as_bytes()).unwrap();
    let a = pb::Authorization {
        approver_id: "oidc|alice".into(),
        role: "operator".into(),
        approved_at_ns: NOW - 1_000_000_000,
        credential_ref: "oidc-attested:hitl-oidc:f7555c90334453d46ee8a5dc766ed6973fccdbb4e9b19cb1a81eeaa8d55f00937ec19cd3a92cf392f9d3c4dee76f6f9a2314c5a3e0e28afdcbbdba217afc380a".into(),
    };
    let v = i
        .verify_hold_authorization(HITL, "hold-1", true, &a, NOW, MAX_AGE_MS)
        .unwrap();
    assert_eq!(v.approver_id, "oidc|alice");
}
