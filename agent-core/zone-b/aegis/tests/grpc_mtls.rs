//! End-to-end gRPC tests over real mutual TLS. Certificates are generated
//! in-test with rcgen (throwaway CA, server and client certs).

use std::sync::Arc;
use std::time::Duration;

use aegis::config::TlsPaths;
use aegis::hex;
use aegis::identity::{approval_text, Identities};
use aegis::pb::aegis_client::AegisClient;
use aegis::pb::{self, DecisionStatus, KillSwitchLevel, ReasonCode};
use aegis::server::{load_tls, serve_on};
use aegis::service::{AegisService, ServiceOptions};
use aegis::signing::{Algorithm, SignError, Signer};
use aegis::testkit::{Rig, RigOptions, NOW_NS, SHARE};
use ed25519_dalek::{Signer as _, SigningKey};
use rcgen::{
    BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, KeyPair,
    KeyUsagePurpose,
};
use tokio::sync::oneshot;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Identity};
use tonic::Code;

struct Ca {
    cert: rcgen::Certificate,
    key: KeyPair,
}

fn new_ca(name: &str) -> Ca {
    let key = KeyPair::generate().unwrap();
    let mut p = CertificateParams::new(vec![]).unwrap();
    p.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    p.distinguished_name.push(DnType::CommonName, name);
    p.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    let cert = p.self_signed(&key).unwrap();
    Ca { cert, key }
}

/// (cert pem, key pem) for a leaf signed by `ca`.
fn leaf(ca: &Ca, cn: &str, server: bool) -> (String, String) {
    let key = KeyPair::generate().unwrap();
    let mut p = CertificateParams::new(vec!["localhost".to_owned()]).unwrap();
    p.distinguished_name.push(DnType::CommonName, cn);
    p.extended_key_usages = vec![if server {
        ExtendedKeyUsagePurpose::ServerAuth
    } else {
        ExtendedKeyUsagePurpose::ClientAuth
    }];
    let cert = p.signed_by(&key, &ca.cert, &ca.key).unwrap();
    (cert.pem(), key.serialize_pem())
}

fn approver_key(seed: u8) -> SigningKey {
    SigningKey::from_bytes(&[seed; 32])
}

fn identities() -> Arc<Identities> {
    let pk = |s: u8| hex::encode(approver_key(s).verifying_key().as_bytes());
    let json = format!(
        r#"{{"peers": {{"cognitive-core": ["signal-submitter"],
                       "operator-console": ["operator","kill-trigger","kill-reset","hold-resolver","state-reader"],
                       "execution-motor": ["state-reader","execution-reporter"]}},
            "approvers": {{"alice": {{"roles": ["operator"], "ed25519_pubkey_hex": "{}"}},
                           "bob": {{"roles": ["operator"], "ed25519_pubkey_hex": "{}"}}}}}}"#,
        pk(1),
        pk(2)
    );
    Arc::new(Identities::from_bytes(json.as_bytes()).unwrap())
}

struct Server {
    port: u16,
    ca: Ca,
    _shutdown: oneshot::Sender<()>,
    _dir: tempfile::TempDir,
    rig: Rig,
}

fn options(max_concurrency: usize, submit_ms: u64) -> ServiceOptions {
    ServiceOptions {
        insecure_dev: false,
        max_concurrency,
        max_watchers: 4,
        submit_timeout: Duration::from_millis(submit_ms),
        rpc_timeout: Duration::from_secs(5),
    }
}

async fn start(rig_opts: RigOptions, opts: ServiceOptions, tls: bool) -> Server {
    let rig = Rig::build(rig_opts);
    let ca = new_ca("aegis-test-ca");
    let dir = tempfile::tempdir().unwrap();
    let (cert, key) = leaf(&ca, "aegis-server", true);
    let paths = TlsPaths {
        cert: dir.path().join("server.pem"),
        key: dir.path().join("server.key"),
        client_ca: dir.path().join("ca.pem"),
    };
    std::fs::write(&paths.cert, cert).unwrap();
    std::fs::write(&paths.key, key).unwrap();
    std::fs::write(&paths.client_ca, ca.cert.pem()).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (tx, rx) = oneshot::channel::<()>();
    let service = AegisService::new(rig.engine.clone(), identities(), opts);
    let tls_cfg = tls.then(|| load_tls(&paths).unwrap());
    tokio::spawn(async move {
        let _ = serve_on(listener, tls_cfg, service, Duration::from_secs(5), async {
            let _ = rx.await;
        })
        .await;
    });
    Server {
        port,
        ca,
        _shutdown: tx,
        _dir: dir,
        rig,
    }
}

async fn client(
    s: &Server,
    cn: Option<&str>,
) -> Result<AegisClient<Channel>, tonic::transport::Error> {
    let mut tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(s.ca.cert.pem()))
        .domain_name("localhost");
    if let Some(cn) = cn {
        let (cert, key) = leaf(&s.ca, cn, false);
        tls = tls.identity(Identity::from_pem(cert, key));
    }
    let channel = Channel::from_shared(format!("https://localhost:{}", s.port))
        .unwrap()
        .tls_config(tls)?
        .connect_timeout(Duration::from_secs(5))
        .connect()
        .await?;
    Ok(AegisClient::new(channel))
}

fn sign_approval(seed: u8, trigger: &str, who: &str, at: i64) -> pb::Authorization {
    let text = approval_text(trigger, who, "operator", at);
    pb::Authorization {
        approver_id: who.into(),
        role: "operator".into(),
        approved_at_ns: at,
        credential_ref: hex::encode(&approver_key(seed).sign(text.as_bytes()).to_bytes()),
    }
}

#[tokio::test]
async fn submit_signal_over_mtls_is_approved_and_the_attestation_verifies() {
    let s = start(RigOptions::default(), options(8, 2_000), true).await;
    let mut c = client(&s, Some("cognitive-core")).await.unwrap();
    let d = c
        .submit_signal(s.rig.signal(1, 10))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        d.decision,
        DecisionStatus::DecisionApproved as i32,
        "{:?}",
        d.reasons
    );
    let (order, att) = (d.order.unwrap(), d.attestation.unwrap());
    let side = aegis::signing::verify::verify_attestation(
        &order,
        &att,
        NOW_NS + 1,
        &s.rig.verifier(),
        aegis::signing::verify::ShortPolicy::Refuse,
    );
    assert_eq!(side, Ok(aegis::domain::Side::Buy));
}

#[tokio::test]
async fn a_client_without_a_certificate_is_rejected() {
    let s = start(RigOptions::default(), options(8, 2_000), true).await;
    let outcome = match client(&s, None).await {
        Err(_) => Err(()),
        Ok(mut c) => c
            .submit_signal(s.rig.signal(1, 10))
            .await
            .map(|_| ())
            .map_err(|_| ()),
    };
    assert!(
        outcome.is_err(),
        "no client certificate must never reach a handler"
    );
    assert_eq!(s.rig.engine.aegis_state().open_holds, 0);
    assert!(s
        .rig
        .portfolio_store
        .saved()
        .unwrap()
        .open_orders
        .is_empty());
}

#[tokio::test]
async fn a_client_certificate_from_an_untrusted_ca_is_rejected() {
    let s = start(RigOptions::default(), options(8, 2_000), true).await;
    let rogue = new_ca("rogue-ca");
    let (cert, key) = leaf(&rogue, "cognitive-core", false);
    let tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(s.ca.cert.pem()))
        .domain_name("localhost")
        .identity(Identity::from_pem(cert, key));
    let attempt = Channel::from_shared(format!("https://localhost:{}", s.port))
        .unwrap()
        .tls_config(tls)
        .unwrap()
        .connect_timeout(Duration::from_secs(5))
        .connect()
        .await;
    let outcome = match attempt {
        Err(_) => Err(()),
        Ok(ch) => AegisClient::new(ch)
            .submit_signal(s.rig.signal(1, 10))
            .await
            .map(|_| ())
            .map_err(|_| ()),
    };
    assert!(outcome.is_err());
    assert!(s
        .rig
        .portfolio_store
        .saved()
        .unwrap()
        .open_orders
        .is_empty());
}

#[tokio::test]
async fn valid_certificates_without_the_role_or_unlisted_are_permission_denied() {
    let s = start(RigOptions::default(), options(8, 2_000), true).await;
    let mut core = client(&s, Some("cognitive-core")).await.unwrap();
    let trigger = pb::TriggerKillSwitchRequest {
        level: KillSwitchLevel::KillLevelHard as i32,
        ..Default::default()
    };
    assert_eq!(
        core.trigger_kill_switch(trigger).await.unwrap_err().code(),
        Code::PermissionDenied
    );
    assert_eq!(
        core.get_kill_switch_state(pb::Empty {})
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert_eq!(
        s.rig.kill.effective_level(),
        Some(KillSwitchLevel::KillLevelNormal)
    );
    let mut stranger = client(&s, Some("stranger")).await.unwrap();
    assert_eq!(
        stranger
            .submit_signal(s.rig.signal(1, 10))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    let mut motor = client(&s, Some("execution-motor")).await.unwrap();
    assert_eq!(
        motor
            .submit_signal(s.rig.signal(2, 10))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert!(s
        .rig
        .portfolio_store
        .saved()
        .unwrap()
        .open_orders
        .is_empty());
}

#[tokio::test]
async fn kill_switch_trigger_watch_and_signed_reset_over_the_wire() {
    let s = start(RigOptions::default(), options(8, 2_000), true).await;
    let mut op = client(&s, Some("operator-console")).await.unwrap();
    let mut watch = op
        .watch_kill_switch_state(pb::Empty {})
        .await
        .unwrap()
        .into_inner();
    let first = watch.message().await.unwrap().unwrap();
    assert_eq!(
        first.effective_level,
        KillSwitchLevel::KillLevelNormal as i32
    );

    let st = op
        .trigger_kill_switch(pb::TriggerKillSwitchRequest {
            level: KillSwitchLevel::KillLevelLogic as i32,
            reason: "drill".into(),
            actor_id: "ops".into(),
            evidence_ref: "INC-1".into(),
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(st.effective_level, KillSwitchLevel::KillLevelLogic as i32);
    let pushed = tokio::time::timeout(Duration::from_secs(5), watch.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(
        pushed.effective_level,
        KillSwitchLevel::KillLevelLogic as i32
    );
    let id = st.latches[0].trigger_id.clone();

    let mut core = client(&s, Some("cognitive-core")).await.unwrap();
    let d = core
        .submit_signal(s.rig.signal(1, 10))
        .await
        .unwrap()
        .into_inner();
    assert_ne!(d.decision, DecisionStatus::DecisionApproved as i32);
    assert!(d
        .reasons
        .contains(&(ReasonCode::ReasonKillSwitchActive as i32)));

    let at = s.rig.now_ns();
    // one approver is not enough for LOGIC
    let one = op
        .reset_kill_switch(pb::ResetKillSwitchRequest {
            trigger_id: id.clone(),
            approvals: vec![sign_approval(1, &id, "alice", at)],
            root_cause_ref: "RCA-1".into(),
            note: String::new(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(!one.accepted && !one.refusal_reason.is_empty());
    // a forged approval (alice's name, bob's key) is refused
    let mut forged = sign_approval(2, &id, "alice", at);
    forged.approver_id = "alice".into();
    let bad = op
        .reset_kill_switch(pb::ResetKillSwitchRequest {
            trigger_id: id.clone(),
            approvals: vec![forged, sign_approval(2, &id, "bob", at)],
            root_cause_ref: "RCA-1".into(),
            note: String::new(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(!bad.accepted);
    assert_eq!(
        s.rig.kill.effective_level(),
        Some(KillSwitchLevel::KillLevelLogic)
    );
    // two distinct signed operators + root cause: accepted
    let ok = op
        .reset_kill_switch(pb::ResetKillSwitchRequest {
            trigger_id: id.clone(),
            approvals: vec![
                sign_approval(1, &id, "alice", at),
                sign_approval(2, &id, "bob", at),
            ],
            root_cause_ref: "RCA-1".into(),
            note: String::new(),
        })
        .await
        .unwrap()
        .into_inner();
    assert!(ok.accepted, "{}", ok.refusal_reason);
    assert_eq!(
        ok.state.unwrap().effective_level,
        KillSwitchLevel::KillLevelNormal as i32
    );
    let pushed = tokio::time::timeout(Duration::from_secs(5), watch.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(
        pushed.effective_level,
        KillSwitchLevel::KillLevelNormal as i32
    );
}

#[tokio::test]
async fn heartbeat_is_bound_to_the_authenticated_identity() {
    let s = start(RigOptions::default(), options(8, 2_000), true).await;
    let mut op = client(&s, Some("operator-console")).await.unwrap();
    let denied = op
        .heartbeat(pb::HeartbeatRequest {
            operator_id: "someone-else".into(),
            client_ts_ns: 0,
        })
        .await;
    assert_eq!(denied.unwrap_err().code(), Code::PermissionDenied);
    let ok = op
        .heartbeat(pb::HeartbeatRequest {
            operator_id: "operator-console".into(),
            client_ts_ns: 0,
        })
        .await;
    assert!(ok.is_ok());
    let empty = op.heartbeat(pb::HeartbeatRequest::default()).await;
    assert_eq!(empty.unwrap_err().code(), Code::InvalidArgument);
}

#[tokio::test]
async fn execution_reports_state_and_hold_rpcs_work_for_their_roles() {
    let s = start(
        RigOptions {
            cold_start: true,
            ..RigOptions::default()
        },
        options(8, 2_000),
        true,
    )
    .await;
    let mut core = client(&s, Some("cognitive-core")).await.unwrap();
    let held = core
        .submit_signal(s.rig.signal(1, 10))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(held.decision, DecisionStatus::DecisionHeldForHuman as i32);
    let mut op = client(&s, Some("operator-console")).await.unwrap();
    let denied = op
        .resolve_hold(pb::ResolveHoldRequest {
            hold_id: held.hold_id.clone(),
            operator_id: "not-me".into(),
            approve: true,
            ..Default::default()
        })
        .await;
    assert_eq!(denied.unwrap_err().code(), Code::PermissionDenied);
    let unknown = op
        .resolve_hold(pb::ResolveHoldRequest {
            hold_id: "nope".into(),
            operator_id: "operator-console".into(),
            approve: true,
            ..Default::default()
        })
        .await;
    assert_eq!(unknown.unwrap_err().code(), Code::NotFound);
    let released = op
        .resolve_hold(pb::ResolveHoldRequest {
            hold_id: held.hold_id,
            operator_id: "operator-console".into(),
            approve: true,
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(released.decision, DecisionStatus::DecisionApproved as i32);

    let mut motor = client(&s, Some("execution-motor")).await.unwrap();
    let id = released.order.unwrap().order_id;
    let ack = motor
        .report_execution(pb::ExecutionReport {
            order_id: id.clone(),
            signal_id: id,
            symbol: "AAPL".into(),
            side: pb::OrderSide::OrderBuy as i32,
            status: pb::OrderStatus::OrderFilled as i32,
            filled_qty_nanos: 10 * SHARE,
            ..Default::default()
        })
        .await
        .unwrap()
        .into_inner();
    assert!(ack.ok, "{}", ack.detail);
    let state = motor
        .get_aegis_state(pb::Empty {})
        .await
        .unwrap()
        .into_inner();
    assert!(state.hsm_ok && state.audit_sink_ok);
    assert_eq!(state.limits_config_sha256, s.rig.limits.sha256_hex);
    assert_eq!(state.open_holds, 0);
}

struct SlowSigner(aegis::signing::dev::DevEd25519Signer);
impl Signer for SlowSigner {
    fn key_id(&self) -> &str {
        self.0.key_id()
    }
    fn algorithm(&self) -> Algorithm {
        Algorithm::Ed25519
    }
    fn sign(&self, d: &[u8; 32]) -> Result<Vec<u8>, SignError> {
        std::thread::sleep(Duration::from_millis(400));
        self.0.sign(d)
    }
}

#[tokio::test]
async fn deadlines_and_concurrency_bounds_fail_closed() {
    let slow = Arc::new(SlowSigner(
        aegis::signing::dev::DevEd25519Signer::from_seed(
            aegis::config::Environment::Test,
            [42; 32],
        )
        .unwrap(),
    ));
    let s = start(
        RigOptions {
            signer: Some(slow),
            ..RigOptions::default()
        },
        options(8, 100),
        true,
    )
    .await;
    let mut core = client(&s, Some("cognitive-core")).await.unwrap();
    let late = core.submit_signal(s.rig.signal(1, 10)).await.unwrap_err();
    assert_eq!(
        late.code(),
        Code::DeadlineExceeded,
        "the caller treats this as REJECT"
    );

    let busy = start(RigOptions::default(), options(0, 2_000), true).await;
    let mut c = client(&busy, Some("cognitive-core")).await.unwrap();
    assert_eq!(
        c.submit_signal(busy.rig.signal(1, 10))
            .await
            .unwrap_err()
            .code(),
        Code::ResourceExhausted
    );
}

#[tokio::test]
async fn insecure_dev_mode_serves_plaintext_without_authentication() {
    let mut o = options(8, 2_000);
    o.insecure_dev = true;
    let s = start(RigOptions::default(), o, false).await;
    let ch = Channel::from_shared(format!("http://127.0.0.1:{}", s.port))
        .unwrap()
        .connect()
        .await
        .unwrap();
    let mut c = AegisClient::new(ch);
    let d = c
        .submit_signal(s.rig.signal(1, 10))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(d.decision, DecisionStatus::DecisionApproved as i32);
}
