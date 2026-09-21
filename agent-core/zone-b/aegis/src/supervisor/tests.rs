use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use super::*;
use crate::clock::ManualClock;

const WINDOW_MS: u64 = 60_000;

#[derive(Clone, Default)]
struct FakeLink {
    probe_ok: Arc<AtomicBool>,
    trip_ok: Arc<AtomicBool>,
    trips: Arc<Mutex<Vec<(String, String)>>>,
}

impl FakeLink {
    fn healthy() -> FakeLink {
        let l = FakeLink::default();
        l.probe_ok.store(true, Ordering::SeqCst);
        l.trip_ok.store(true, Ordering::SeqCst);
        l
    }
    fn set_probe(&self, ok: bool) {
        self.probe_ok.store(ok, Ordering::SeqCst);
    }
    fn set_trip(&self, ok: bool) {
        self.trip_ok.store(ok, Ordering::SeqCst);
    }
    fn trip_count(&self) -> usize {
        self.trips.lock().unwrap().len()
    }
}

#[tonic::async_trait]
impl AegisLink for FakeLink {
    async fn probe(&self) -> Result<(), LinkError> {
        if self.probe_ok.load(Ordering::SeqCst) {
            Ok(())
        } else {
            Err(LinkError("down".into()))
        }
    }
    async fn trip_hard(&self, reason: &str, evidence_ref: &str) -> Result<(), LinkError> {
        if self.trip_ok.load(Ordering::SeqCst) {
            self.trips
                .lock()
                .unwrap()
                .push((reason.to_owned(), evidence_ref.to_owned()));
            Ok(())
        } else {
            Err(LinkError("cannot reach aegis".into()))
        }
    }
}

fn rig() -> (Supervisor<FakeLink, ManualClock>, FakeLink, ManualClock) {
    let link = FakeLink::healthy();
    let clock = ManualClock::new(1_000_000_000);
    let sup = Supervisor::new(link.clone(), clock.clone(), WINDOW_MS).unwrap();
    (sup, link, clock)
}

#[tokio::test]
async fn healthy_probes_never_trip() {
    let (mut sup, link, clock) = rig();
    for _ in 0..200 {
        clock.advance_ms(1_000);
        assert_eq!(sup.step().await.unwrap(), Outcome::Healthy);
    }
    assert_eq!(link.trip_count(), 0);
}

#[tokio::test]
async fn trips_hard_only_after_the_window_of_failed_probes() {
    let (mut sup, link, clock) = rig();
    link.set_probe(false);
    clock.advance_ms(1_000);
    assert_eq!(
        sup.step().await.unwrap(),
        Outcome::Degraded {
            failed_for_ms: 1_000
        }
    );
    clock.advance_ms(59_000); // exactly 60 s since the last success
    assert_eq!(
        sup.step().await.unwrap(),
        Outcome::Degraded {
            failed_for_ms: 60_000
        },
        "the window is strictly greater than 60 s"
    );
    assert_eq!(link.trip_count(), 0);
    clock.advance_ms(1);
    assert_eq!(sup.step().await.unwrap(), Outcome::Tripped);
    assert_eq!(link.trip_count(), 1);
    let (reason, evidence) = link.trips.lock().unwrap()[0].clone();
    assert!(reason.contains("liveness"), "{reason}");
    assert!(evidence.starts_with("supervisor:"), "{evidence}");
}

#[tokio::test]
async fn one_successful_probe_restarts_the_window() {
    let (mut sup, link, clock) = rig();
    link.set_probe(false);
    clock.advance_ms(59_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::Degraded { .. }
    ));
    link.set_probe(true);
    clock.advance_ms(500);
    assert_eq!(sup.step().await.unwrap(), Outcome::Healthy);
    link.set_probe(false);
    clock.advance_ms(59_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::Degraded { .. }
    ));
    assert_eq!(link.trip_count(), 0);
}

#[tokio::test]
async fn an_aegis_that_is_down_from_the_start_is_tripped_after_one_window() {
    let link = FakeLink::healthy();
    link.set_probe(false);
    let clock = ManualClock::new(5_000_000_000);
    let mut sup = Supervisor::new(link.clone(), clock.clone(), WINDOW_MS).unwrap();
    clock.advance_ms(60_001);
    assert_eq!(sup.step().await.unwrap(), Outcome::Tripped);
}

#[tokio::test]
async fn a_tripped_outage_latches_once_then_rearms_after_recovery() {
    let (mut sup, link, clock) = rig();
    link.set_probe(false);
    clock.advance_ms(61_000);
    assert_eq!(sup.step().await.unwrap(), Outcome::Tripped);
    for _ in 0..5 {
        clock.advance_ms(1_000);
        assert_eq!(sup.step().await.unwrap(), Outcome::AlreadyTripped);
    }
    assert_eq!(link.trip_count(), 1, "one latch per outage");
    link.set_probe(true);
    assert_eq!(sup.step().await.unwrap(), Outcome::Healthy);
    link.set_probe(false);
    clock.advance_ms(61_000);
    assert_eq!(sup.step().await.unwrap(), Outcome::Tripped);
    assert_eq!(link.trip_count(), 2);
}

#[tokio::test]
async fn an_undelivered_trip_is_retried_until_it_lands() {
    let (mut sup, link, clock) = rig();
    link.set_probe(false);
    link.set_trip(false);
    clock.advance_ms(61_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::TripFailed { .. }
    ));
    clock.advance_ms(10_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::TripFailed { .. }
    ));
    link.set_trip(true);
    clock.advance_ms(1_000);
    assert_eq!(sup.step().await.unwrap(), Outcome::Tripped);
    assert_eq!(link.trip_count(), 1);
}

#[tokio::test]
async fn a_trip_that_stays_undeliverable_for_another_window_ends_the_process() {
    let (mut sup, link, clock) = rig();
    link.set_probe(false);
    link.set_trip(false);
    clock.advance_ms(61_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::TripFailed { .. }
    ));
    clock.advance_ms(60_000);
    assert!(matches!(
        sup.step().await.unwrap(),
        Outcome::TripFailed { .. }
    ));
    clock.advance_ms(1);
    let err = sup.step().await.unwrap_err();
    assert!(matches!(err, SupervisorError::TripUndeliverable { .. }));
    assert_eq!(err.exit_code(), 3);
}

struct BrokenClock;

impl Clock for BrokenClock {
    fn now_ns(&self) -> Result<i64, ClockError> {
        Err(ClockError)
    }
}

#[tokio::test]
async fn an_unreadable_clock_is_a_fatal_supervisor_error() {
    let err = Supervisor::new(FakeLink::healthy(), BrokenClock, WINDOW_MS)
        .err()
        .expect("a supervisor without a clock cannot start");
    assert_eq!(err.exit_code(), 1);
}

#[tokio::test]
async fn setup_and_config_errors_map_to_exit_code_2() {
    assert_eq!(SupervisorError::Setup("x".into()).exit_code(), 2);
    assert_eq!(
        SupervisorError::Config(ConfigError::Missing("X")).exit_code(),
        2
    );
}

#[tokio::test]
async fn unreadable_tls_files_fail_setup_instead_of_running_blind() {
    use config::{SupervisorTls, SupervisorTlsPaths};
    let cfg = SupervisorConfig {
        env: crate::config::Environment::Test,
        target: "https://127.0.0.1:1".into(),
        tls: SupervisorTls::Mutual(SupervisorTlsPaths {
            ca: "/nonexistent/ca.pem".into(),
            cert: "/nonexistent/c.pem".into(),
            key: "/nonexistent/k.pem".into(),
            domain: None,
        }),
        trip_after_ms: WINDOW_MS,
        probe_interval: Duration::from_secs(1),
        probe_timeout: Duration::from_secs(1),
    };
    let err = run(cfg, std::future::pending()).await.unwrap_err();
    assert!(matches!(err, SupervisorError::Setup(_)), "{err}");
    assert_eq!(err.exit_code(), 2);
}

#[tokio::test]
async fn run_stops_cleanly_on_shutdown() {
    let (sup, _link, _clock) = rig();
    let (tx, rx) = tokio::sync::oneshot::channel::<()>();
    let handle = tokio::spawn(sup.run(Duration::from_millis(5), async {
        let _ = rx.await;
    }));
    tokio::time::sleep(Duration::from_millis(30)).await;
    tx.send(()).unwrap();
    assert!(handle.await.unwrap().is_ok());
}

#[test]
fn monotonic_clock_never_goes_backwards_and_starts_positive() {
    let c = MonotonicClock::new();
    let a = c.now_ns().unwrap();
    let b = c.now_ns().unwrap();
    assert!(a > 0 && b >= a);
}
