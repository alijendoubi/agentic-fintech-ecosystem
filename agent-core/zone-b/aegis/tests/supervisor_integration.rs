//! The Supervisor against a REAL in-process Aegis over real mutual TLS, plus
//! the `aegis supervisor` process contract (non-zero exit on its own errors).
//! The liveness window runs on a fake clock, so nothing sleeps for 60 s.

mod common;

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use aegis::clock::ManualClock;
use aegis::config::Environment;
use aegis::killswitch::watchdog::SUPERVISOR_ACTOR;
use aegis::pb::KillSwitchLevel;
use aegis::supervisor::config::{SupervisorTls, SupervisorTlsPaths};
use aegis::supervisor::{
    AegisLink, GrpcAegis, LinkError, Outcome, Supervisor, SupervisorConfig, SupervisorError,
};
use aegis::testkit::RigOptions;
use common::{leaf, new_ca, start, Ca, Server};

const WINDOW_MS: u64 = 60_000;

/// Real gRPC link whose probe can be made to fail (a hung or unreachable
/// Aegis), while the trip still uses the real channel.
struct BlockableProbe {
    inner: GrpcAegis,
    blocked: Arc<AtomicBool>,
}

#[tonic::async_trait]
impl AegisLink for BlockableProbe {
    async fn probe(&self) -> Result<(), LinkError> {
        if self.blocked.load(Ordering::SeqCst) {
            return Err(LinkError("probe blocked by the test".into()));
        }
        self.inner.probe().await
    }
    async fn trip_hard(&self, reason: &str, evidence_ref: &str) -> Result<(), LinkError> {
        self.inner.trip_hard(reason, evidence_ref).await
    }
}

fn write_client_files(dir: &Path, ca: &Ca, cn: &str) -> SupervisorTlsPaths {
    let (cert, key) = leaf(ca, cn, false);
    let paths = SupervisorTlsPaths {
        ca: dir.join("sup-ca.pem"),
        cert: dir.join(format!("{cn}.pem")),
        key: dir.join(format!("{cn}.key")),
        domain: None,
    };
    std::fs::write(&paths.ca, ca.cert.pem()).unwrap();
    std::fs::write(&paths.cert, cert).unwrap();
    std::fs::write(&paths.key, key).unwrap();
    paths
}

fn config(target: String, tls: SupervisorTlsPaths) -> SupervisorConfig {
    SupervisorConfig {
        env: Environment::Test,
        target,
        tls: SupervisorTls::Mutual(tls),
        trip_after_ms: WINDOW_MS,
        probe_interval: Duration::from_secs(1),
        probe_timeout: Duration::from_secs(2),
    }
}

fn link_to(s: &Server, cn: &str) -> GrpcAegis {
    let tls = write_client_files(s.dir.path(), &s.ca, cn);
    GrpcAegis::connect(&config(format!("https://localhost:{}", s.port), tls)).unwrap()
}

#[tokio::test]
async fn a_healthy_aegis_is_probed_over_mtls_and_never_tripped() {
    let s = start(RigOptions::default()).await;
    let clock = ManualClock::new(1_000_000_000);
    let mut sup = Supervisor::new(link_to(&s, "supervisor"), clock.clone(), WINDOW_MS).unwrap();
    for _ in 0..5 {
        clock.advance_ms(30_000);
        assert_eq!(sup.step().await.unwrap(), Outcome::Healthy);
    }
    assert_eq!(
        s.rig.kill.effective_level(),
        Some(KillSwitchLevel::KillLevelNormal)
    );
}

#[tokio::test]
async fn sixty_seconds_of_failed_probes_latch_hard_as_supervisor_liveness() {
    let s = start(RigOptions::default()).await;
    let blocked = Arc::new(AtomicBool::new(false));
    let link = BlockableProbe {
        inner: link_to(&s, "supervisor"),
        blocked: blocked.clone(),
    };
    let clock = ManualClock::new(1_000_000_000);
    let mut sup = Supervisor::new(link, clock.clone(), WINDOW_MS).unwrap();
    assert_eq!(sup.step().await.unwrap(), Outcome::Healthy);

    blocked.store(true, Ordering::SeqCst);
    clock.advance_ms(60_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::Degraded { .. }
    ));
    assert_eq!(
        s.rig.kill.effective_level(),
        Some(KillSwitchLevel::KillLevelNormal),
        "not yet: the window is strictly longer than 60 s"
    );
    clock.advance_ms(1);
    assert_eq!(sup.step().await.unwrap(), Outcome::Tripped);

    let state = s.rig.kill.state();
    assert_eq!(state.effective_level, KillSwitchLevel::KillLevelHard as i32);
    let latch = state
        .latches
        .iter()
        .find(|l| l.level == KillSwitchLevel::KillLevelHard as i32)
        .expect("a HARD latch");
    assert!(
        latch.actor_id.contains(SUPERVISOR_ACTOR),
        "actor was {:?}",
        latch.actor_id
    );
    assert!(latch.reason.contains("liveness"), "{:?}", latch.reason);
}

#[tokio::test]
async fn a_supervisor_certificate_without_kill_trigger_cannot_trip_and_fails_loudly() {
    let s = start(RigOptions::default()).await;
    let blocked = Arc::new(AtomicBool::new(true));
    let link = BlockableProbe {
        inner: link_to(&s, "read-only-supervisor"),
        blocked,
    };
    let clock = ManualClock::new(1_000_000_000);
    let mut sup = Supervisor::new(link, clock.clone(), WINDOW_MS).unwrap();
    clock.advance_ms(61_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::TripFailed { .. }
    ));
    assert_eq!(
        s.rig.kill.effective_level(),
        Some(KillSwitchLevel::KillLevelNormal)
    );
    clock.advance_ms(60_001);
    let err = sup.step().await.unwrap_err();
    assert!(matches!(err, SupervisorError::TripUndeliverable { .. }));
    assert_ne!(err.exit_code(), 0);
}

#[tokio::test]
async fn an_unreachable_aegis_is_detected_by_real_probes_and_reported_when_the_trip_cannot_land() {
    // A port nothing listens on.
    let port = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let ca = new_ca("unreachable-ca");
    let dir = tempfile::tempdir().unwrap();
    let tls = write_client_files(dir.path(), &ca, "supervisor");
    let link = GrpcAegis::connect(&config(format!("https://localhost:{port}"), tls)).unwrap();
    let clock = ManualClock::new(1_000_000_000);
    let mut sup = Supervisor::new(link, clock.clone(), WINDOW_MS).unwrap();
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::Degraded { .. }
    ));
    clock.advance_ms(61_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::TripFailed { .. }
    ));
    clock.advance_ms(60_001);
    assert!(matches!(
        sup.step().await.unwrap_err(),
        SupervisorError::TripUndeliverable { .. }
    ));
}

fn run_binary(args: &[&str], env: &[(&str, &str)]) -> std::process::Output {
    std::process::Command::new(env!("CARGO_BIN_EXE_aegis"))
        .args(args)
        .env_clear()
        .envs(env.iter().copied())
        .output()
        .unwrap()
}

#[test]
fn the_supervisor_process_exits_non_zero_when_it_cannot_read_its_config() {
    let out = run_binary(&["supervisor"], &[]);
    assert_eq!(out.status.code(), Some(2), "no config at all");

    let out = run_binary(
        &["supervisor"],
        &[
            ("AEGIS_ENV", "test"),
            ("AEGIS_SUPERVISOR_TARGET", "https://localhost:1"),
            ("AEGIS_SUPERVISOR_TLS_CA", "/nonexistent/ca.pem"),
            ("AEGIS_SUPERVISOR_TLS_CERT", "/nonexistent/c.pem"),
            ("AEGIS_SUPERVISOR_TLS_KEY", "/nonexistent/k.pem"),
        ],
    );
    assert_eq!(out.status.code(), Some(2), "unreadable TLS files");

    let out = run_binary(
        &["supervisor"],
        &[
            ("AEGIS_ENV", "test"),
            ("AEGIS_SUPERVISOR_TARGET", "https://localhost:1"),
            ("AEGIS_SUPERVISOR_TLS_CA", "/nonexistent/ca.pem"),
            ("AEGIS_SUPERVISOR_TLS_CERT", "/nonexistent/c.pem"),
            ("AEGIS_SUPERVISOR_TLS_KEY", "/nonexistent/k.pem"),
            ("AEGIS_SUPERVISOR_TRIP_AFTER_MS", "999999"),
        ],
    );
    assert_eq!(
        out.status.code(),
        Some(2),
        "trip window above the spec value"
    );
}

#[test]
fn an_unknown_subcommand_does_not_start_the_server_silently() {
    let out = run_binary(&["superviser"], &[]);
    assert_eq!(out.status.code(), Some(2));
}
