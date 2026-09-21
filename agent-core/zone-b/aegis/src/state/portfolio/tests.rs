use super::*;
use crate::pb::OrderStatus;

const SHARE: i64 = 1_000_000_000;

fn px(_: &str) -> Option<Nanos> {
    Some(Nanos::new(100 * SHARE))
}

fn report(id: &str, sell: bool, status: OrderStatus, cum: i64) -> FillReport<'_> {
    FillReport {
        order_id: id,
        symbol: "AAPL",
        is_sell: sell,
        status: status as i32,
        cum_filled: cum,
        now_ns: 0,
    }
}

#[test]
fn reservation_counts_as_pending_until_reported_terminal() {
    let mut p = Portfolio::default();
    p.reserve("o1", "AAPL", false, Nanos::new(10 * SHARE), 1_000)
        .unwrap();
    let v = p.exposure_view("AAPL", &px);
    assert_eq!((v.pending_buy, v.pending_sell), (i128::from(10 * SHARE), 0));
    p.apply_fill_report(&report("o1", false, OrderStatus::OrderPartial, 4 * SHARE))
        .unwrap();
    let v = p.exposure_view("AAPL", &px);
    assert_eq!(v.position, Nanos::new(4 * SHARE));
    assert_eq!(
        v.pending_buy,
        i128::from(6 * SHARE),
        "only the unfilled remainder is pending"
    );
    p.apply_fill_report(&report("o1", false, OrderStatus::OrderFilled, 10 * SHARE))
        .unwrap();
    let v = p.exposure_view("AAPL", &px);
    assert_eq!((v.position, v.pending_buy), (Nanos::new(10 * SHARE), 0));
    assert!(p.open_orders.is_empty());
}

#[test]
fn duplicate_reports_are_idempotent_and_decreasing_fills_are_refused() {
    let mut p = Portfolio::default();
    p.reserve("o1", "AAPL", false, Nanos::new(10 * SHARE), 1_000)
        .unwrap();
    let r = report("o1", false, OrderStatus::OrderPartial, 4 * SHARE);
    p.apply_fill_report(&r).unwrap();
    p.apply_fill_report(&r).unwrap();
    assert_eq!(p.positions["AAPL"], 4 * SHARE);
    let back = report("o1", false, OrderStatus::OrderPartial, 3 * SHARE);
    assert_eq!(p.apply_fill_report(&back), Err(ReportError::DecreasingFill));
    assert_eq!(
        p.apply_fill_report(&report("o1", false, OrderStatus::OrderPartial, -1)),
        Err(ReportError::NegativeFill)
    );
    assert_eq!(
        p.apply_fill_report(&report("o1", true, OrderStatus::OrderPartial, 5 * SHARE)),
        Err(ReportError::Mismatch)
    );
}

#[test]
fn sells_reduce_the_position_and_cancel_releases_the_remainder() {
    let mut p = Portfolio::default();
    p.positions.insert("AAPL".into(), 20 * SHARE);
    p.reserve("s1", "AAPL", true, Nanos::new(15 * SHARE), 1_000)
        .unwrap();
    p.apply_fill_report(&report("s1", true, OrderStatus::OrderCancelled, 5 * SHARE))
        .unwrap();
    assert_eq!(p.positions["AAPL"], 15 * SHARE);
    assert_eq!(p.exposure_view("AAPL", &px).pending_sell, 0);
}

#[test]
fn unknown_orders_still_move_the_position() {
    let mut p = Portfolio::default();
    p.apply_fill_report(&report("ghost", false, OrderStatus::OrderFilled, 2 * SHARE))
        .unwrap();
    assert_eq!(p.positions["AAPL"], 2 * SHARE);
    assert!(p.open_orders.is_empty());
}

#[test]
fn position_overflow_is_refused() {
    let mut p = Portfolio::default();
    p.positions.insert("AAPL".into(), i64::MAX);
    assert_eq!(
        p.apply_fill_report(&report("o", false, OrderStatus::OrderFilled, 1)),
        Err(ReportError::Overflow)
    );
}

#[test]
fn unreported_reservations_expire_but_submitted_ones_do_not() {
    let mut p = Portfolio::default();
    p.reserve("never", "AAPL", false, Nanos::new(SHARE), 100)
        .unwrap();
    p.reserve("sent", "AAPL", false, Nanos::new(SHARE), 100)
        .unwrap();
    p.apply_fill_report(&report("sent", false, OrderStatus::OrderSent, 0))
        .unwrap();
    p.expire_reservations(100);
    assert_eq!(p.open_orders.len(), 2, "not yet expired at the boundary");
    p.expire_reservations(101);
    assert!(p.open_orders.contains_key("sent") && !p.open_orders.contains_key("never"));
}

#[test]
fn others_gross_uses_worst_case_and_needs_every_mark() {
    let mut p = Portfolio::default();
    p.positions.insert("MSFT".into(), 10 * SHARE);
    p.reserve("m1", "MSFT", false, Nanos::new(5 * SHARE), 1_000)
        .unwrap();
    let v = p.exposure_view("AAPL", &px);
    assert_eq!(v.others_gross, Some(i128::from(15 * SHARE) * 100));
    let v = p.exposure_view("AAPL", &|_| None);
    assert_eq!(v.others_gross, None);
    // the traded symbol itself is excluded and needs no mark
    let v = p.exposure_view("MSFT", &|_| None);
    assert_eq!(v.others_gross, Some(0));
}

#[test]
fn equity_is_unavailable_until_the_first_report_of_the_day() {
    let mut p = Portfolio::default();
    let day = NANOS_PER_DAY;
    assert_eq!(p.equity_today(day), None);
    p.record_equity(1_000, day + 5);
    p.record_equity(900, day + 10);
    assert_eq!(
        p.equity_today(day + 20),
        Some((Nanos::new(1_000), Nanos::new(900)))
    );
    assert_eq!(
        p.equity_today(2 * day + 1),
        None,
        "new day: fail closed until re-baselined"
    );
    p.record_equity(880, 2 * day + 2);
    assert_eq!(
        p.equity_today(2 * day + 3),
        Some((Nanos::new(880), Nanos::new(880)))
    );
}

#[test]
fn size_history_nearest_rank_p95_and_window() {
    let mut p = Portfolio::default();
    for i in 1..=100 {
        p.record_size("AAPL", Nanos::new(i), 1_000 + i);
    }
    let h = p.size_history("AAPL", 2_000, 30);
    assert_eq!(h.count, 100);
    assert_eq!(h.p95, Some(Nanos::new(95)));
    let old = 1_000 + 31 * NANOS_PER_DAY;
    assert_eq!(p.size_history("AAPL", old, 30).count, 0);
    assert_eq!(p.size_history("AAPL", old, 30).p95, None);
    p.record_size("MSFT", Nanos::new(7), 5);
    let one = p.size_history("MSFT", 6, 30);
    assert_eq!((one.count, one.p95), (1, Some(Nanos::new(7))));
}

#[test]
fn size_samples_are_bounded() {
    let mut p = Portfolio::default();
    for i in 0..(MAX_SIZE_SAMPLES as i64 + 50) {
        p.record_size("AAPL", Nanos::new(i), i);
    }
    assert_eq!(p.size_samples["AAPL"].len(), MAX_SIZE_SAMPLES);
}

#[test]
fn file_store_round_trips_and_corruption_is_an_error() {
    let dir = tempfile::tempdir().unwrap();
    let s = FilePortfolioStore::new(dir.path(), StateInit::Bootstrap);
    assert_eq!(s.load().unwrap(), Portfolio::default());
    let mut p = Portfolio::default();
    p.positions.insert("AAPL".into(), 5);
    p.record_equity(10, 1);
    s.save(&p).unwrap();
    assert_eq!(s.load().unwrap(), p);
    std::fs::write(dir.path().join(STATE_FILE), b"nope").unwrap();
    assert!(s.load().is_err());
}

#[test]
fn missing_snapshot_in_an_existing_state_dir_is_an_error_not_a_flat_portfolio() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(dir.path().join("kill_state.json"), b"{}").unwrap();
    let s = FilePortfolioStore::new(dir.path(), StateInit::Existing);
    assert!(s.load().is_err());
    assert!(s.load().is_err(), "stays an error on every load");
    assert!(!dir.path().join(STATE_FILE).exists(), "nothing is created");
}

#[test]
fn bootstrap_is_one_shot_and_persists_the_empty_snapshot() {
    let dir = tempfile::tempdir().unwrap();
    let s = FilePortfolioStore::new(dir.path(), StateInit::Bootstrap);
    assert_eq!(s.load().unwrap(), Portfolio::default());
    assert!(dir.path().join(STATE_FILE).exists());
    // the next start finds the file
    let next = FilePortfolioStore::new(dir.path(), StateInit::Existing);
    assert_eq!(next.load().unwrap(), Portfolio::default());
    // deleting it under a running process does not re-arm the bootstrap
    std::fs::remove_file(dir.path().join(STATE_FILE)).unwrap();
    assert!(s.load().is_err());
}

#[test]
fn memory_store_simulates_failed_saves() {
    let m = MemoryPortfolioStore::default();
    m.set_fail_saves(true);
    assert!(m.save(&Portfolio::default()).is_err());
    m.set_fail_saves(false);
    assert!(m.save(&Portfolio::default()).is_ok() && m.saved().is_some());
}
