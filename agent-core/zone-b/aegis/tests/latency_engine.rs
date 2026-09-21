//! Full decision path (validation, C01-C19, signing, exposure reservation,
//! audit, replay record) with in-memory stores: isolates compute cost from
//! filesystem latency (see `app_startup.rs` for the file-backed numbers).
//!
//!   cargo test --release --test latency_engine -- --nocapture

use std::time::Instant;

use aegis::pb::DecisionStatus;
use aegis::testkit::Rig;

const ITERATIONS: u64 = 400;

#[test]
fn engine_approval_path_with_in_memory_stores() {
    let rig = Rig::default();
    let mut samples = Vec::new();
    for n in 1..=ITERATIONS {
        // keep the token buckets full and the market data fresh
        rig.clock.advance_ms(600);
        rig.refresh_market();
        let s = rig.signal(n, 1);
        let t = Instant::now();
        let d = rig.engine.submit_signal(&s);
        let el = t.elapsed();
        assert_eq!(
            d.decision,
            DecisionStatus::DecisionApproved as i32,
            "{n}: {:?}",
            d.reasons
        );
        samples.push(el);
    }
    samples.sort();
    let p50 = samples[samples.len() / 2];
    let p99 = samples[samples.len() * 99 / 100];
    let max = *samples.last().unwrap();
    println!(
        "engine full path, in-memory stores, ed25519 dev signer: n={} p50={p50:?} p99={p99:?} max={max:?} (release={})",
        samples.len(),
        !cfg!(debug_assertions)
    );
    let budget_ms = if cfg!(debug_assertions) { 50 } else { 10 };
    assert!(
        p99.as_millis() < budget_ms,
        "p99 {p99:?} exceeds {budget_ms} ms"
    );
}
