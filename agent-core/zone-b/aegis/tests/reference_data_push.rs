//! `PushReferenceData` end to end over real mutual TLS: the feed that makes C08
//! pass, and every way it must refuse (stale, out of order, unauthorised).

mod common;

use aegis::pb::{self, DecisionStatus, ReasonCode};
use aegis::state::refdata::ReferenceData;
use aegis::testkit::{RigOptions, NOW_NS, SHARE};
use common::{client, start, Server};
use tonic::Code;

const MS: i64 = 1_000_000;

async fn no_market_server() -> Server {
    start(RigOptions {
        no_market: true,
        ..RigOptions::default()
    })
    .await
}

fn snapshot(as_of_ns: i64) -> pb::ReferenceSnapshot {
    pb::ReferenceSnapshot {
        symbol: "AAPL".into(),
        mid_price_nanos: 150 * SHARE,
        adv_30d_nanos: 1_000_000 * SHARE,
        as_of_ns,
        is_stale: false,
    }
}

fn regime(at: i64) -> pb::RegimeLabelPacket {
    pb::RegimeLabelPacket {
        label: pb::RegimeLabel::TrendingBull as i32,
        confidence: 0.9,
        timestamp_ns: at,
        state_index: 0,
    }
}

fn full_push(as_of_ns: i64) -> pb::PushReferenceDataRequest {
    pb::PushReferenceDataRequest {
        snapshots: vec![snapshot(as_of_ns)],
        regime: Some(regime(as_of_ns)),
    }
}

fn c08_failed(d: &pb::AegisDecision) -> bool {
    d.results.iter().any(|r| {
        r.control_id == "C08"
            && !r.passed
            && r.reason == ReasonCode::ReasonStaleReferencePrice as i32
    })
}

#[tokio::test]
async fn c08_fails_without_a_feed_and_passes_after_a_valid_push() {
    let s = no_market_server().await;
    let mut core = client(&s, "cognitive-core").await;
    let mut feed = client(&s, "market-data").await;

    let before = core
        .submit_signal(s.rig.signal(1, 10))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(before.decision, DecisionStatus::DecisionRejected as i32);
    assert!(c08_failed(&before), "no feed: C08 must fail closed");

    let resp = feed
        .push_reference_data(full_push(NOW_NS))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(resp.applied_snapshots, 1);
    assert!(resp.regime_applied);
    assert!(resp.rejected.is_empty(), "{:?}", resp.rejected);

    let after = core
        .submit_signal(s.rig.signal(2, 10))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(
        after.decision,
        DecisionStatus::DecisionApproved as i32,
        "{:?}",
        after.results
    );
    assert!(after
        .results
        .iter()
        .any(|r| r.control_id == "C08" && r.passed));
}

#[tokio::test]
async fn pushed_data_ages_out_and_c08_fails_closed_again() {
    let s = no_market_server().await;
    let mut core = client(&s, "cognitive-core").await;
    let mut feed = client(&s, "market-data").await;
    feed.push_reference_data(full_push(NOW_NS)).await.unwrap();
    // Default max_ref_age_ms is 1000: no fresh push for 2 s.
    s.rig.clock.advance_ms(2_000);
    let d = core
        .submit_signal(s.rig.signal(1, 10))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(d.decision, DecisionStatus::DecisionRejected as i32);
    assert!(c08_failed(&d));
    assert!(!s.rig.engine.aegis_state().reference_data_fresh);
}

#[tokio::test]
async fn stale_pushes_are_refused_and_never_stored() {
    let s = no_market_server().await;
    let mut feed = client(&s, "market-data").await;
    let resp = feed
        .push_reference_data(full_push(NOW_NS - 1_001 * MS))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(resp.applied_snapshots, 0);
    assert!(
        resp.regime_applied,
        "the regime is inside its own, longer bound"
    );
    assert_eq!(resp.rejected.len(), 1);
    assert_eq!(resp.rejected[0].key, "AAPL");
    assert_eq!(resp.rejected[0].reason, "stale");
    assert!(s.rig.refdata.price("AAPL").is_none());
    assert!(!s.rig.engine.aegis_state().reference_data_fresh);

    let mut flagged = snapshot(NOW_NS);
    flagged.is_stale = true;
    feed.push_reference_data(pb::PushReferenceDataRequest {
        snapshots: vec![flagged],
        regime: None,
    })
    .await
    .unwrap();
    assert!(s.rig.refdata.price("AAPL").unwrap().is_stale);
    assert!(!s.rig.engine.aegis_state().reference_data_fresh);
}

#[tokio::test]
async fn out_of_order_pushes_never_overwrite_newer_data() {
    let s = no_market_server().await;
    let mut feed = client(&s, "market-data").await;
    feed.push_reference_data(full_push(NOW_NS - 10 * MS))
        .await
        .unwrap();

    let mut older = full_push(NOW_NS - 20 * MS);
    older.snapshots[0].mid_price_nanos = 1;
    let resp = feed.push_reference_data(older).await.unwrap().into_inner();
    assert_eq!(resp.applied_snapshots, 0);
    assert!(!resp.regime_applied);
    let reasons: Vec<_> = resp.rejected.iter().map(|r| r.reason.as_str()).collect();
    assert_eq!(reasons, vec!["out_of_order", "out_of_order"]);
    assert_eq!(
        s.rig.refdata.price("AAPL").unwrap().mid.get(),
        150 * SHARE,
        "the newer stored price survives"
    );

    let newer = feed
        .push_reference_data(full_push(NOW_NS - 5 * MS))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(newer.applied_snapshots, 1);
    assert_eq!(
        s.rig.refdata.price("AAPL").unwrap().ingested_at_ns,
        NOW_NS - 5 * MS
    );
}

#[tokio::test]
async fn only_the_market_data_writer_role_may_push() {
    let s = no_market_server().await;
    for cn in [
        "cognitive-core",
        "supervisor",
        "execution-motor",
        "unlisted-peer",
    ] {
        let mut c = client(&s, cn).await;
        let err = c.push_reference_data(full_push(NOW_NS)).await.unwrap_err();
        assert_eq!(err.code(), Code::PermissionDenied, "{cn}");
    }
    assert!(s.rig.refdata.price("AAPL").is_none());
    assert!(s.rig.refdata.regime().is_none());

    // And the writer role grants nothing else.
    let mut feed = client(&s, "market-data").await;
    assert_eq!(
        feed.submit_signal(s.rig.signal(1, 10))
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
    assert_eq!(
        feed.get_kill_switch_state(pb::Empty {})
            .await
            .unwrap_err()
            .code(),
        Code::PermissionDenied
    );
}

#[tokio::test]
async fn empty_and_oversized_pushes_are_invalid_arguments() {
    let s = no_market_server().await;
    let mut feed = client(&s, "market-data").await;
    let empty = pb::PushReferenceDataRequest::default();
    assert_eq!(
        feed.push_reference_data(empty).await.unwrap_err().code(),
        Code::InvalidArgument
    );
    let huge = pb::PushReferenceDataRequest {
        snapshots: vec![snapshot(NOW_NS); aegis::state::ingest::MAX_SNAPSHOTS_PER_PUSH + 1],
        regime: None,
    };
    assert_eq!(
        feed.push_reference_data(huge).await.unwrap_err().code(),
        Code::InvalidArgument
    );
    assert!(s.rig.refdata.price("AAPL").is_none());
}
