//! Start-up assembly against the REAL file-backed stores (kill state, replay
//! log, portfolio snapshot, audit WAL): restart persistence, fail-to-HARD on
//! lost or corrupt state, refusal to start without limits, and the timing of
//! the full approval path including fsyncs.

use std::collections::HashMap;
use std::path::Path;
use std::time::Instant;

use aegis::app::{build_app, App};
use aegis::clock::{Clock, SystemClock};
use aegis::config::RuntimeConfig;
use aegis::controls::{RefPrice, RegimeView};
use aegis::money::Nanos;
use aegis::pb::{self, DecisionStatus, KillSwitchLevel};
use aegis::state::portfolio::{FilePortfolioStore, Portfolio, PortfolioStore};
use aegis::testkit::{limits_json, uuid_n, SHARE};

/// Session open all week so the wall clock cannot flake the test.
fn all_week_limits() -> String {
    limits_json().replace(
        r#""session": {"weekdays_utc": [1,2,3,4,5], "start_minute_utc": 810, "end_minute_utc": 1200}"#,
        r#""session": {"weekdays_utc": [1,2,3,4,5,6,7], "start_minute_utc": 0, "end_minute_utc": 1440}"#,
    )
}

const IDENTITIES: &str = r#"{"peers": {}, "approvers": {}}"#;

struct Env {
    _dir: tempfile::TempDir,
    cfg: RuntimeConfig,
    state: std::path::PathBuf,
}

fn env_with(limits: Option<&str>) -> Env {
    let dir = tempfile::tempdir().unwrap();
    let state = dir.path().join("state");
    if let Some(l) = limits {
        std::fs::write(dir.path().join("limits.json"), l).unwrap();
    }
    std::fs::write(dir.path().join("identities.json"), IDENTITIES).unwrap();
    let vars: HashMap<&str, String> = HashMap::from([
        ("AEGIS_ENV", "test".to_owned()),
        ("AEGIS_INSECURE_DEV", "1".to_owned()),
        ("AEGIS_SIGNER", "dev".to_owned()),
        (
            "AEGIS_LIMITS_FILE",
            dir.path().join("limits.json").display().to_string(),
        ),
        (
            "AEGIS_IDENTITIES_FILE",
            dir.path().join("identities.json").display().to_string(),
        ),
        ("AEGIS_STATE_DIR", state.display().to_string()),
    ]);
    let cfg = RuntimeConfig::from_lookup(&|k| vars.get(k).cloned()).unwrap();
    Env {
        _dir: dir,
        cfg,
        state,
    }
}

fn level(app: &App) -> Option<KillSwitchLevel> {
    app.engine.kill().effective_level()
}

#[test]
fn refuses_to_start_without_limits_or_with_an_incomplete_file() {
    let none = env_with(None);
    assert!(build_app(&none.cfg).is_err(), "missing limits file");
    let incomplete = env_with(Some(r#"{"symbols": {}}"#));
    assert!(build_app(&incomplete.cfg).is_err(), "incomplete limits");
    let empty = env_with(Some("{}"));
    assert!(build_app(&empty.cfg).is_err());
    assert!(
        !empty.state.join("kill_state.json").exists(),
        "no state is created before limits validate"
    );
}

#[test]
fn first_boot_is_normal_and_a_latch_survives_restart() {
    let e = env_with(Some(&all_week_limits()));
    {
        let app = build_app(&e.cfg).unwrap();
        assert_eq!(level(&app), Some(KillSwitchLevel::KillLevelNormal));
        app.engine
            .kill()
            .trigger(KillSwitchLevel::KillLevelLogic as i32, "test", "test")
            .unwrap();
        assert_eq!(level(&app), Some(KillSwitchLevel::KillLevelLogic));
    }
    let again = build_app(&e.cfg).unwrap();
    assert_eq!(
        level(&again),
        Some(KillSwitchLevel::KillLevelLogic),
        "a restart never lowers the level"
    );
    let wal = std::fs::read_to_string(e.state.join("audit.wal")).unwrap();
    assert!(wal.contains("\"event\":\"kill_transition\""));
}

#[test]
fn deleted_or_corrupt_kill_state_starts_at_hard() {
    let e = env_with(Some(&all_week_limits()));
    drop(build_app(&e.cfg).unwrap());
    assert!(e.state.join("kill_state.json").exists());
    std::fs::remove_file(e.state.join("kill_state.json")).unwrap();
    assert_eq!(
        level(&build_app(&e.cfg).unwrap()),
        Some(KillSwitchLevel::KillLevelHard),
        "deleted state next to other files"
    );

    let e = env_with(Some(&all_week_limits()));
    drop(build_app(&e.cfg).unwrap());
    std::fs::write(e.state.join("kill_state.json"), b"{corrupt").unwrap();
    assert_eq!(
        level(&build_app(&e.cfg).unwrap()),
        Some(KillSwitchLevel::KillLevelHard)
    );
    let quarantined = std::fs::read_dir(&e.state).unwrap().any(|f| {
        f.unwrap()
            .file_name()
            .to_string_lossy()
            .contains("corrupt-")
    });
    assert!(
        quarantined,
        "the unreadable file is preserved for forensics"
    );
}

fn seed_portfolio(dir: &Path) {
    let now = SystemClock.now_ns().unwrap();
    let mut p = Portfolio::default();
    for i in 0..30 {
        p.record_size("AAPL", Nanos::new(100 * SHARE), now - 3_600_000_000_000 + i);
    }
    p.record_equity(1_000_000 * SHARE, now);
    FilePortfolioStore::new(dir).save(&p).unwrap();
}

fn signal(n: u64, now: i64) -> pb::TradeSignal {
    pb::TradeSignal {
        signal_id: uuid_n(n),
        symbol: "AAPL".into(),
        created_at_ns: now - 50_000_000,
        side: pb::SignalSide::Buy as i32,
        omega: 0.8,
        regime: pb::RegimeLabel::TrendingBull as i32,
        regime_confidence: 0.9,
        valid_until_ns: now + 4_000_000_000,
        quantity_nanos: SHARE,
        price_limit_nanos: 150 * SHARE,
        ..pb::TradeSignal::default()
    }
}

fn feed(app: &App) {
    let now = SystemClock.now_ns().unwrap();
    app.refdata
        .set_price(
            "AAPL",
            RefPrice {
                mid: Nanos::new(150 * SHARE),
                adv: Nanos::new(1_000_000 * SHARE),
                ingested_at_ns: now,
                is_stale: false,
            },
        )
        .unwrap();
    app.refdata
        .set_regime(RegimeView {
            label: pb::RegimeLabel::TrendingBull,
            confidence: 0.9,
            at_ns: now,
        })
        .unwrap();
}

#[test]
fn file_backed_replay_protection_survives_a_restart_and_nothing_trades_without_a_feed() {
    let e = env_with(Some(&all_week_limits()));
    std::fs::create_dir_all(&e.state).unwrap();
    let app = build_app(&e.cfg).unwrap();
    let now = SystemClock.now_ns().unwrap();
    let no_feed = app.engine.submit_signal(&signal(1, now));
    assert_eq!(
        no_feed.decision,
        DecisionStatus::DecisionRejected as i32,
        "no reference data: reject everything"
    );
    drop(app);
    seed_portfolio(&e.state);
    let app = build_app(&e.cfg).unwrap();
    feed(&app);
    let now = SystemClock.now_ns().unwrap();
    let s = signal(2, now);
    let first = app.engine.submit_signal(&s);
    assert_eq!(
        first.decision,
        DecisionStatus::DecisionApproved as i32,
        "{:?}",
        first.reasons
    );
    drop(app);
    let app = build_app(&e.cfg).unwrap();
    feed(&app);
    let replay = app.engine.submit_signal(&s);
    assert_eq!(
        replay, first,
        "the durable replay log returns the original decision after a restart"
    );
    let mut mutated = s.clone();
    mutated.quantity_nanos += 1;
    let d = app.engine.submit_signal(&mutated);
    assert!(d
        .reasons
        .contains(&(pb::ReasonCode::ReasonReplayPayloadMismatch as i32)));
}

#[test]
fn full_approval_path_timing_with_real_fsyncs_and_signing() {
    let e = env_with(Some(&all_week_limits()));
    std::fs::create_dir_all(&e.state).unwrap();
    drop(build_app(&e.cfg).unwrap());
    seed_portfolio(&e.state);
    let app = build_app(&e.cfg).unwrap();
    // the rate limit (5 burst per symbol) is real: measure distinct symbols-free by
    // spacing with the token refill, i.e. only the approved ones are timed
    let mut samples = Vec::new();
    for n in 0..24u64 {
        feed(&app);
        let now = SystemClock.now_ns().unwrap();
        let t = Instant::now();
        let d = app.engine.submit_signal(&signal(100 + n, now));
        let el = t.elapsed();
        if d.decision == DecisionStatus::DecisionApproved as i32 {
            samples.push(el);
        }
        std::thread::sleep(std::time::Duration::from_millis(600));
    }
    assert!(
        samples.len() >= 20,
        "enough approvals to time: {}",
        samples.len()
    );
    samples.sort();
    let p50 = samples[samples.len() / 2];
    let max = *samples.last().unwrap();
    println!(
        "full approval path (file-backed, ed25519 dev signer, 3 fsyncs): n={} p50={p50:?} max={max:?}",
        samples.len()
    );
    // The tail is dominated by the host filesystem's fsync latency (measured on
    // Docker Desktop: max above 50 ms). Only the median is asserted; the numbers
    // are reported, and the 50 ms budget is NOT claimed for the tail.
    assert!(
        p50.as_millis() < 50,
        "median full path exceeded the 50 ms budget: {p50:?}"
    );
}
