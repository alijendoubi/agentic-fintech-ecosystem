//! A `SubmitSignal` whose caller already saw DEADLINE_EXCEEDED must not go on
//! to sign and reserve exposure in the background: nobody will ever use that
//! attestation, so the reservation would only starve later signals.

use std::sync::Arc;
use std::time::Duration;

use aegis::config::Environment;
use aegis::identity::Identities;
use aegis::pb::Aegis;
use aegis::service::{AegisService, ServiceOptions};
use aegis::signing::dev::DevEd25519Signer;
use aegis::signing::{Algorithm, SignError, Signer};
use aegis::testkit::{Rig, RigOptions};
use tonic::{Code, Request};

/// Dev signer that takes `delay` to sign (a slow HSM).
struct SlowSigner {
    inner: DevEd25519Signer,
    delay: Duration,
}

impl Signer for SlowSigner {
    fn key_id(&self) -> &str {
        self.inner.key_id()
    }
    fn algorithm(&self) -> Algorithm {
        self.inner.algorithm()
    }
    fn sign(&self, digest: &[u8; 32]) -> Result<Vec<u8>, SignError> {
        std::thread::sleep(self.delay);
        self.inner.sign(digest)
    }
}

fn service_over(rig: &Rig, submit_timeout: Duration) -> AegisService {
    let ids = Identities::from_bytes(br#"{"peers": {}, "approvers": {}}"#).unwrap();
    AegisService::new(
        rig.engine.clone(),
        Arc::new(ids),
        ServiceOptions {
            insecure_dev: true,
            max_concurrency: 4,
            max_watchers: 2,
            submit_timeout,
            rpc_timeout: Duration::from_secs(2),
        },
    )
}

fn slow_rig(delay: Duration) -> Rig {
    let inner = DevEd25519Signer::from_seed(Environment::Test, [42; 32]).unwrap();
    Rig::build(RigOptions {
        signer: Some(Arc::new(SlowSigner { inner, delay })),
        ..RigOptions::default()
    })
}

#[tokio::test]
async fn a_signal_whose_caller_timed_out_reserves_no_exposure() {
    let rig = slow_rig(Duration::from_millis(400));
    let service = service_over(&rig, Duration::from_millis(100));
    let err = service
        .submit_signal(Request::new(rig.signal(1, 10)))
        .await
        .unwrap_err();
    assert_eq!(err.code(), Code::DeadlineExceeded);
    // let the abandoned blocking task run to completion
    tokio::time::sleep(Duration::from_millis(800)).await;
    let open = rig.portfolio_store.saved().unwrap().open_orders;
    assert!(
        open.is_empty(),
        "an abandoned request must not leave a reservation: {open:?}"
    );
}

#[tokio::test]
async fn the_abandoned_signal_id_can_be_retried_and_approved() {
    let rig = slow_rig(Duration::from_millis(400));
    let service = service_over(&rig, Duration::from_millis(100));
    let s = rig.signal(1, 10);
    let _ = service.submit_signal(Request::new(s.clone())).await;
    tokio::time::sleep(Duration::from_millis(800)).await;
    let patient = service_over(&rig, Duration::from_secs(5));
    let d = patient
        .submit_signal(Request::new(s))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        d.decision,
        aegis::pb::DecisionStatus::DecisionApproved as i32,
        "the cancelled attempt must not have burned the signal id: {:?}",
        d.reasons
    );
}
