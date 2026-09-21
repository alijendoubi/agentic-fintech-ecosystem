//! Freshness (TTL) checks. Fail closed: anything doubtful marks the snapshot stale.
//!
//! Semantics (spec table: L2 1 ms, trade print 5 ms):
//! * The TTL clocks are the **local** clock on both sides: age = `now_ns` (taken
//!   per message, right before the check) minus the local *receive* time of the
//!   data. That measures how long data has been sitting inside this pipeline,
//!   which is what a 1 ms hot-path budget can meaningfully bound. Measuring
//!   `now - exchange_ts` (as the first version did) mostly measures network
//!   latency and clock skew and flags nearly every quote.
//! * Exchange-vs-local clock problems are reported separately, never hidden:
//!   an exchange timestamp far in the future (`.max(0)` used to swallow that) or
//!   a quote whose SIP timestamp is older than `feed_max_lag` are both stale.
//! * A local clock that moved backwards (negative age) is stale, not "fresh".
//! * Comparisons are in nanoseconds; there is no millisecond truncation.
//! * No trade seen yet is not a breach (`trade_recv_ts_ns == 0`); consumers
//!   see `last_trade_ts_ns == 0`.
//!
//! Reasons are static strings so a stale snapshot costs no allocation
//! (except the rare multi-reason join).

use std::borrow::Cow;

use crate::normalizer::MarketSnapshot;

pub const L2_TTL_BREACH: &str = "L2_TTL_BREACH";
pub const TRADE_TTL_BREACH: &str = "TRADE_TTL_BREACH";
pub const FEED_LAG_BREACH: &str = "FEED_LAG_BREACH";
pub const FUTURE_EXCHANGE_TS: &str = "FUTURE_EXCHANGE_TS";

const NS_PER_MS: i64 = 1_000_000;

/// Freshness limits, all in milliseconds.
#[derive(Debug, Clone, Copy)]
pub struct TtlLimits {
    pub l2_max_ms: u64,
    pub trade_max_ms: u64,
    pub feed_max_lag_ms: u64,
    pub future_tolerance_ms: u64,
}

pub struct TtlChecker {
    l2_max_ns: i64,
    trade_max_ns: i64,
    feed_max_lag_ns: i64,
    future_tolerance_ns: i64,
}

fn ms_to_ns_saturating(ms: u64) -> i64 {
    i64::try_from(ms)
        .unwrap_or(i64::MAX)
        .saturating_mul(NS_PER_MS)
}

impl TtlChecker {
    pub fn new(limits: TtlLimits) -> Self {
        Self {
            l2_max_ns: ms_to_ns_saturating(limits.l2_max_ms),
            trade_max_ns: ms_to_ns_saturating(limits.trade_max_ms),
            feed_max_lag_ns: ms_to_ns_saturating(limits.feed_max_lag_ms),
            future_tolerance_ns: ms_to_ns_saturating(limits.future_tolerance_ms),
        }
    }

    /// Set `is_stale` / `stale_reason` on the snapshot.
    pub fn check(&self, snap: &mut MarketSnapshot, now_ns: i64) {
        let mut reasons: [&'static str; 4] = [""; 4];
        let mut n = 0;
        let mut flag = |reason: &'static str| {
            reasons[n] = reason;
            n += 1;
        };

        let l2_age = now_ns.saturating_sub(snap.l2_recv_ts_ns);
        if snap.l2_recv_ts_ns <= 0 || l2_age < 0 || l2_age > self.l2_max_ns {
            flag(L2_TTL_BREACH);
        }

        if snap.trade_recv_ts_ns > 0 {
            let trade_age = now_ns.saturating_sub(snap.trade_recv_ts_ns);
            if trade_age < 0 || trade_age > self.trade_max_ns {
                flag(TRADE_TTL_BREACH);
            }
        }

        let exchange_age = now_ns.saturating_sub(snap.exchange_ts_ns);
        if snap.exchange_ts_ns <= 0 || exchange_age > self.feed_max_lag_ns {
            flag(FEED_LAG_BREACH);
        } else if exchange_age < -self.future_tolerance_ns {
            flag(FUTURE_EXCHANGE_TS);
        }

        snap.is_stale = n > 0;
        snap.stale_reason = join_reasons(&reasons[..n]);
    }
}

fn join_reasons(reasons: &[&'static str]) -> Cow<'static, str> {
    match reasons {
        [] => Cow::Borrowed(""),
        [one] => Cow::Borrowed(one),
        many => Cow::Owned(many.join(";")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 1_700_000_000_000_000_000;

    fn checker() -> TtlChecker {
        TtlChecker::new(TtlLimits {
            l2_max_ms: 1,
            trade_max_ms: 5,
            feed_max_lag_ms: 1_000,
            future_tolerance_ms: 1_000,
        })
    }

    /// A snapshot whose L2 was received `l2_age_ns` before NOW and whose
    /// exchange timestamp equals the L2 receive time (zero network latency).
    fn snap(l2_age_ns: i64, trade_age_ns: Option<i64>) -> MarketSnapshot {
        MarketSnapshot {
            symbol: "TEST".into(),
            ingestion_ts_ns: NOW,
            exchange_ts_ns: NOW - l2_age_ns,
            l2_recv_ts_ns: NOW - l2_age_ns,
            mid_price: 100.0,
            bid_price: 99.9,
            ask_price: 100.1,
            bid_size: 10.0,
            ask_size: 10.0,
            spread: 0.2,
            last_trade_price: 100.0,
            last_trade_size: 100.0,
            last_trade_ts_ns: trade_age_ns.map_or(0, |a| NOW - a),
            trade_recv_ts_ns: trade_age_ns.map_or(0, |a| NOW - a),
            z_score: 0.0,
            mad_score: 0.0,
            z_mad_divergence: false,
            order_flow_imbalance: 0.0,
            realized_volatility: 0.0,
            adv_30d: 0.0,
            warmup: false,
            regime_label: "UNKNOWN".into(),
            regime_confidence: 0.0,
            is_stale: false,
            stale_reason: "".into(),
        }
    }

    #[test]
    fn fresh_within_ttl() {
        let mut s = snap(500_000, Some(2_000_000)); // 0.5ms / 2ms
        checker().check(&mut s, NOW);
        assert!(!s.is_stale, "{}", s.stale_reason);
        assert_eq!(s.stale_reason, "");
    }

    #[test]
    fn l2_breach_is_detected_at_sub_millisecond_resolution() {
        let mut s = snap(1_000_001, None); // 1.000001 ms > 1 ms (ms truncation would pass this)
        checker().check(&mut s, NOW);
        assert!(s.is_stale);
        assert_eq!(s.stale_reason, L2_TTL_BREACH);
    }

    #[test]
    fn l2_exactly_at_limit_is_fresh() {
        let mut s = snap(1_000_000, None);
        checker().check(&mut s, NOW);
        assert!(!s.is_stale);
    }

    #[test]
    fn trade_breach_is_reported_with_spec_reason() {
        let mut s = snap(100, Some(6_000_000));
        checker().check(&mut s, NOW);
        assert!(s.is_stale);
        assert_eq!(s.stale_reason, TRADE_TTL_BREACH);
    }

    #[test]
    fn no_trade_yet_is_not_a_breach() {
        let mut s = snap(100, None);
        checker().check(&mut s, NOW);
        assert!(!s.is_stale);
    }

    #[test]
    fn network_latency_alone_does_not_make_quotes_stale() {
        // SIP timestamp 40 ms old (normal WAN latency) but processed instantly.
        let mut s = snap(100, None);
        s.exchange_ts_ns = NOW - 40_000_000;
        checker().check(&mut s, NOW);
        assert!(!s.is_stale, "{}", s.stale_reason);
    }

    #[test]
    fn feed_lag_beyond_limit_is_stale() {
        let mut s = snap(100, None);
        s.exchange_ts_ns = NOW - 5_000_000_000; // 5 s old (e.g. delayed feed)
        checker().check(&mut s, NOW);
        assert!(s.is_stale);
        assert_eq!(s.stale_reason, FEED_LAG_BREACH);
    }

    #[test]
    fn future_exchange_timestamp_is_not_hidden() {
        let mut s = snap(100, None);
        s.exchange_ts_ns = NOW + 10_000_000_000; // 10 s in the future
        checker().check(&mut s, NOW);
        assert!(s.is_stale);
        assert_eq!(s.stale_reason, FUTURE_EXCHANGE_TS);
        // Small lead within tolerance is fine.
        s.exchange_ts_ns = NOW + 500_000_000;
        checker().check(&mut s, NOW);
        assert!(!s.is_stale);
    }

    #[test]
    fn local_clock_going_backwards_fails_closed() {
        let mut s = snap(0, None);
        s.l2_recv_ts_ns = NOW + 5_000_000; // receive time "after" now
        checker().check(&mut s, NOW);
        assert!(s.is_stale);
        assert!(s.stale_reason.contains(L2_TTL_BREACH));
    }

    #[test]
    fn now_is_per_message_not_per_frame() {
        // Same snapshot judged at two later instants: fresh, then stale.
        let mut s = snap(0, None);
        checker().check(&mut s, NOW + 500_000);
        assert!(!s.is_stale);
        checker().check(&mut s, NOW + 2_000_000);
        assert!(s.is_stale);
    }

    #[test]
    fn multiple_reasons_are_joined_and_state_resets() {
        let mut s = snap(2_000_000, Some(9_000_000));
        checker().check(&mut s, NOW);
        assert_eq!(s.stale_reason, "L2_TTL_BREACH;TRADE_TTL_BREACH");
        let mut fresh = snap(0, None);
        fresh.is_stale = true;
        fresh.stale_reason = "OLD".into();
        checker().check(&mut fresh, NOW);
        assert!(!fresh.is_stale);
        assert_eq!(fresh.stale_reason, "");
    }

    #[test]
    fn missing_timestamps_fail_closed() {
        let mut s = snap(0, None);
        s.l2_recv_ts_ns = 0;
        checker().check(&mut s, NOW);
        assert!(s.is_stale);
    }
}
