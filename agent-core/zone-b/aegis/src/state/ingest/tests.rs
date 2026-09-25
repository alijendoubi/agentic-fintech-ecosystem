use super::*;
use crate::limits::Limits;
use crate::state::refdata::ReferenceData;
use crate::testkit::{limits_json, NOW_NS, SHARE};

fn cfg() -> Limits {
    Limits::from_bytes(limits_json().as_bytes()).unwrap()
}

fn snap(symbol: &str, as_of_ns: i64) -> pb::ReferenceSnapshot {
    pb::ReferenceSnapshot {
        symbol: symbol.into(),
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
        symbol: String::new(),
    }
}

fn symbol_regime(symbol: &str, label: pb::RegimeLabel, at: i64) -> pb::RegimeLabelPacket {
    pb::RegimeLabelPacket {
        label: label as i32,
        symbol: symbol.into(),
        ..regime(at)
    }
}

fn push_symbol_regimes(
    store: &MemoryReferenceData,
    regimes: Vec<pb::RegimeLabelPacket>,
) -> pb::PushReferenceDataResponse {
    let l = cfg();
    ingest(
        store,
        &l.config,
        NOW_NS,
        &pb::PushReferenceDataRequest {
            symbol_regimes: regimes,
            ..pb::PushReferenceDataRequest::default()
        },
    )
}

fn push(
    store: &MemoryReferenceData,
    snaps: Vec<pb::ReferenceSnapshot>,
    r: Option<pb::RegimeLabelPacket>,
) -> pb::PushReferenceDataResponse {
    let l = cfg();
    ingest(
        store,
        &l.config,
        NOW_NS,
        &pb::PushReferenceDataRequest {
            snapshots: snaps,
            regime: r,
            symbol_regimes: vec![],
        },
    )
}

fn reasons(r: &pb::PushReferenceDataResponse) -> Vec<(&str, &str)> {
    r.rejected
        .iter()
        .map(|x| (x.key.as_str(), x.reason.as_str()))
        .collect()
}

#[test]
fn fresh_snapshot_and_regime_are_stored_as_nanos() {
    let store = MemoryReferenceData::default();
    let r = push(
        &store,
        vec![snap("AAPL", NOW_NS - 10)],
        Some(regime(NOW_NS)),
    );
    assert_eq!(r.applied_snapshots, 1);
    assert!(r.regime_applied);
    assert!(r.rejected.is_empty());
    let p = store.price("AAPL").unwrap();
    assert_eq!(p.mid, Nanos::new(150 * SHARE));
    assert_eq!(p.ingested_at_ns, NOW_NS - 10);
    assert_eq!(store.regime().unwrap().label, pb::RegimeLabel::TrendingBull);
}

#[test]
fn stale_snapshot_is_refused_and_nothing_is_stored() {
    let store = MemoryReferenceData::default();
    let max = 1_000 * 1_000_000; // default max_ref_age_ms
    let r = push(&store, vec![snap("AAPL", NOW_NS - max - 1)], None);
    assert_eq!(reasons(&r), vec![("AAPL", REJECT_STALE)]);
    assert!(store.price("AAPL").is_none());
    // Exactly at the bound is accepted.
    let r = push(&store, vec![snap("AAPL", NOW_NS - max)], None);
    assert_eq!(r.applied_snapshots, 1);
}

#[test]
fn stale_regime_uses_its_own_bound() {
    let store = MemoryReferenceData::default();
    let max = 60_000 * 1_000_000;
    let r = push(&store, vec![], Some(regime(NOW_NS - max - 1)));
    assert_eq!(reasons(&r), vec![("regime", REJECT_STALE)]);
    assert!(store.regime().is_none());
}

#[test]
fn future_dated_items_are_refused_beyond_the_skew() {
    let store = MemoryReferenceData::default();
    let skew = 250 * 1_000_000;
    let r = push(
        &store,
        vec![snap("AAPL", NOW_NS + skew + 1)],
        Some(regime(NOW_NS + skew + 1)),
    );
    assert_eq!(
        reasons(&r),
        vec![("AAPL", REJECT_FUTURE), ("regime", REJECT_FUTURE)]
    );
    let r = push(&store, vec![snap("AAPL", NOW_NS + skew)], None);
    assert_eq!(r.applied_snapshots, 1);
}

#[test]
fn out_of_order_and_duplicate_updates_never_replace_newer_data() {
    let store = MemoryReferenceData::default();
    assert_eq!(
        push(
            &store,
            vec![snap("AAPL", NOW_NS - 100)],
            Some(regime(NOW_NS - 100))
        )
        .applied_snapshots,
        1
    );
    let mut older = snap("AAPL", NOW_NS - 200);
    older.mid_price_nanos = 1;
    let dup = snap("AAPL", NOW_NS - 100);
    let r = push(&store, vec![older, dup], Some(regime(NOW_NS - 200)));
    assert_eq!(r.applied_snapshots, 0);
    assert!(!r.regime_applied);
    assert_eq!(
        reasons(&r),
        vec![
            ("AAPL", REJECT_OUT_OF_ORDER),
            ("AAPL", REJECT_OUT_OF_ORDER),
            ("regime", REJECT_OUT_OF_ORDER)
        ]
    );
    assert_eq!(store.price("AAPL").unwrap().mid, Nanos::new(150 * SHARE));
    assert_eq!(store.regime().unwrap().at_ns, NOW_NS - 100);
    // A strictly newer one replaces it.
    assert_eq!(
        push(&store, vec![snap("AAPL", NOW_NS - 50)], None).applied_snapshots,
        1
    );
}

#[test]
fn invalid_and_unknown_items_are_refused_individually() {
    let store = MemoryReferenceData::default();
    let mut zero_mid = snap("AAPL", NOW_NS);
    zero_mid.mid_price_nanos = 0;
    let mut neg_adv = snap("MSFT", NOW_NS);
    neg_adv.adv_30d_nanos = -5;
    let mut no_ts = snap("MSFT", 0);
    no_ts.mid_price_nanos = 1;
    let bad_regime = pb::RegimeLabelPacket {
        confidence: f64::NAN,
        ..regime(NOW_NS)
    };
    let r = push(
        &store,
        vec![
            zero_mid,
            neg_adv,
            no_ts,
            snap("", NOW_NS),
            snap("TSLA", NOW_NS),
            snap("MSFT", NOW_NS),
        ],
        Some(bad_regime),
    );
    assert_eq!(r.applied_snapshots, 1, "only the valid MSFT snapshot");
    assert_eq!(
        reasons(&r),
        vec![
            ("AAPL", REJECT_INVALID),
            ("MSFT", REJECT_INVALID),
            ("MSFT", REJECT_INVALID),
            ("", REJECT_INVALID),
            ("TSLA", REJECT_UNKNOWN_SYMBOL),
            ("regime", REJECT_INVALID)
        ]
    );
    assert!(store.price("AAPL").is_none());
    assert!(store.price("TSLA").is_none());
    for bad in [-1, 99] {
        let r = push(
            &store,
            vec![],
            Some(pb::RegimeLabelPacket {
                label: bad,
                ..regime(NOW_NS)
            }),
        );
        assert_eq!(reasons(&r), vec![("regime", REJECT_INVALID)]);
    }
}

#[test]
fn a_producer_stale_flag_is_stored_so_c08_fails_closed() {
    let store = MemoryReferenceData::default();
    let mut s = snap("AAPL", NOW_NS);
    s.is_stale = true;
    assert_eq!(push(&store, vec![s], None).applied_snapshots, 1);
    assert!(store.price("AAPL").unwrap().is_stale);
    assert!(!store.has_fresh_data(NOW_NS, 1_000_000_000));
}

// ---- ALI-158: per-symbol regime labels ----

#[test]
fn per_symbol_regimes_are_stored_per_symbol() {
    let store = MemoryReferenceData::default();
    let r = push_symbol_regimes(
        &store,
        vec![
            symbol_regime("AAPL", pb::RegimeLabel::TrendingBull, NOW_NS - 20),
            symbol_regime("MSFT", pb::RegimeLabel::Crisis, NOW_NS - 10),
        ],
    );
    assert_eq!(r.applied_symbol_regimes, 2);
    assert!(r.rejected.is_empty(), "{:?}", reasons(&r));
    assert!(!r.regime_applied, "no universe-wide label was sent");
    assert_eq!(
        store.regime_for("AAPL").unwrap().label,
        pb::RegimeLabel::TrendingBull,
        "MSFT's newer label must not overwrite AAPL's"
    );
    assert_eq!(
        store.regime_for("MSFT").unwrap().label,
        pb::RegimeLabel::Crisis
    );
    assert!(store.regime().is_none());
}

#[test]
fn per_symbol_regimes_fail_closed_item_by_item() {
    let store = MemoryReferenceData::default();
    let max = i64::try_from(cfg().config.timings.max_regime_age_ms).unwrap() * 1_000_000;
    let r = push_symbol_regimes(
        &store,
        vec![
            symbol_regime("", pb::RegimeLabel::TrendingBull, NOW_NS),
            symbol_regime("ZZZZ", pb::RegimeLabel::TrendingBull, NOW_NS),
            symbol_regime("AAPL", pb::RegimeLabel::TrendingBull, NOW_NS - max - 1),
            pb::RegimeLabelPacket {
                confidence: 1.5,
                ..symbol_regime("MSFT", pb::RegimeLabel::TrendingBull, NOW_NS)
            },
        ],
    );
    assert_eq!(r.applied_symbol_regimes, 0);
    assert_eq!(
        reasons(&r),
        vec![
            ("regime:", REJECT_INVALID),
            ("regime:ZZZZ", REJECT_UNKNOWN_SYMBOL),
            ("regime:AAPL", REJECT_STALE),
            ("regime:MSFT", REJECT_INVALID),
        ]
    );
    assert!(store.regime_for("AAPL").is_none());
    assert!(store.regime_for("MSFT").is_none());
}

#[test]
fn per_symbol_regime_is_monotonic_per_symbol() {
    let store = MemoryReferenceData::default();
    push_symbol_regimes(
        &store,
        vec![symbol_regime(
            "AAPL",
            pb::RegimeLabel::TrendingBull,
            NOW_NS - 10,
        )],
    );
    let r = push_symbol_regimes(
        &store,
        vec![
            symbol_regime("AAPL", pb::RegimeLabel::Crisis, NOW_NS - 10),
            symbol_regime("MSFT", pb::RegimeLabel::Crisis, NOW_NS - 50),
        ],
    );
    assert_eq!(reasons(&r), vec![("regime:AAPL", REJECT_OUT_OF_ORDER)]);
    assert_eq!(
        r.applied_symbol_regimes, 1,
        "an older MSFT label is still MSFT's first"
    );
    assert_eq!(
        store.regime_for("AAPL").unwrap().label,
        pb::RegimeLabel::TrendingBull
    );
}
