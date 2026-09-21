//! Shared helpers for the mTLS integration tests of the reference-data feed and
//! the Supervisor: a throwaway CA, leaf certificates, an in-process Aegis
//! server over real mutual TLS, and clients.
#![allow(dead_code)]

use std::sync::Arc;
use std::time::Duration;

use aegis::config::TlsPaths;
use aegis::identity::Identities;
use aegis::pb::aegis_client::AegisClient;
use aegis::server::{load_tls, serve_on};
use aegis::service::{AegisService, ServiceOptions};
use aegis::testkit::{Rig, RigOptions};
use rcgen::{
    BasicConstraints, CertificateParams, DnType, ExtendedKeyUsagePurpose, IsCa, KeyPair,
    KeyUsagePurpose,
};
use tokio::sync::oneshot;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Identity};

pub struct Ca {
    pub cert: rcgen::Certificate,
    pub key: KeyPair,
}

pub fn new_ca(name: &str) -> Ca {
    let key = KeyPair::generate().unwrap();
    let mut p = CertificateParams::new(vec![]).unwrap();
    p.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    p.distinguished_name.push(DnType::CommonName, name);
    p.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
    let cert = p.self_signed(&key).unwrap();
    Ca { cert, key }
}

/// (cert pem, key pem) for a leaf signed by `ca`.
pub fn leaf(ca: &Ca, cn: &str, server: bool) -> (String, String) {
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

pub fn identities() -> Arc<Identities> {
    let json = r#"{"peers": {
        "cognitive-core": ["signal-submitter"],
        "market-data": ["market-data-writer"],
        "supervisor": ["state-reader", "kill-trigger"],
        "read-only-supervisor": ["state-reader"],
        "execution-motor": ["state-reader", "execution-reporter"]},
      "approvers": {}}"#;
    Arc::new(Identities::from_bytes(json.as_bytes()).unwrap())
}

pub struct Server {
    pub port: u16,
    pub ca: Ca,
    pub rig: Rig,
    pub dir: tempfile::TempDir,
    pub server_cert_pem: String,
    shutdown: Option<oneshot::Sender<()>>,
}

impl Server {
    /// Stop serving (in-flight and new connections fail afterwards).
    pub fn stop(&mut self) {
        if let Some(tx) = self.shutdown.take() {
            let _ = tx.send(());
        }
    }
}

pub fn options() -> ServiceOptions {
    ServiceOptions {
        insecure_dev: false,
        max_concurrency: 8,
        max_watchers: 4,
        submit_timeout: Duration::from_secs(2),
        rpc_timeout: Duration::from_secs(5),
    }
}

/// Start an in-process Aegis over mTLS with the reference-data feed attached.
pub async fn start(rig_opts: RigOptions) -> Server {
    let rig = Rig::build(rig_opts);
    let ca = new_ca("aegis-test-ca");
    let dir = tempfile::tempdir().unwrap();
    let (cert, key) = leaf(&ca, "aegis-server", true);
    let paths = TlsPaths {
        cert: dir.path().join("server.pem"),
        key: dir.path().join("server.key"),
        client_ca: dir.path().join("ca.pem"),
    };
    std::fs::write(&paths.cert, &cert).unwrap();
    std::fs::write(&paths.key, key).unwrap();
    std::fs::write(&paths.client_ca, ca.cert.pem()).unwrap();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (tx, rx) = oneshot::channel::<()>();
    let service = AegisService::new(rig.engine.clone(), identities(), options())
        .with_refdata(rig.refdata.clone());
    let tls_cfg = Some(load_tls(&paths).unwrap());
    tokio::spawn(async move {
        let _ = serve_on(listener, tls_cfg, service, Duration::from_secs(5), async {
            let _ = rx.await;
        })
        .await;
    });
    Server {
        port,
        ca,
        rig,
        dir,
        server_cert_pem: cert,
        shutdown: Some(tx),
    }
}

pub async fn client(s: &Server, cn: &str) -> AegisClient<Channel> {
    let mut tls = ClientTlsConfig::new()
        .ca_certificate(Certificate::from_pem(s.ca.cert.pem()))
        .domain_name("localhost");
    let (cert, key) = leaf(&s.ca, cn, false);
    tls = tls.identity(Identity::from_pem(cert, key));
    let channel = Channel::from_shared(format!("https://localhost:{}", s.port))
        .unwrap()
        .tls_config(tls)
        .unwrap()
        .connect_timeout(Duration::from_secs(5))
        .connect()
        .await
        .unwrap();
    AegisClient::new(channel)
}
