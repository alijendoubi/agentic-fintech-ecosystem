use crate::normalizer::MarketSnapshot;

/// TTL freshness rules per the blueprint signal freshness table.
pub struct TtlChecker {
    /// Max age for L2 order book data (milliseconds)
    l2_max_ms: u64,
    /// Max age for trade print data (milliseconds)
    trade_max_ms: u64,
}

impl TtlChecker {
    pub fn new(l2_max_ms: u64, trade_max_ms: u64) -> Self {
        Self {
            l2_max_ms,
            trade_max_ms,
        }
    }

    /// Check freshness of a snapshot. Mutates is_stale and stale_reason in place.
    pub fn check(&self, snapshot: &mut MarketSnapshot, now_ns: i64) {
        snapshot.is_stale = false;
        snapshot.stale_reason.clear();

        // L2 quote freshness
        let l2_age_ms = ((now_ns - snapshot.exchange_ts_ns).max(0) as u64) / 1_000_000;
        if l2_age_ms > self.l2_max_ms {
            snapshot.is_stale = true;
            snapshot.stale_reason = format!(
                "L2_TTL_BREACH: age={}ms max={}ms",
                l2_age_ms, self.l2_max_ms
            );
            return; // first violation wins
        }

        // Trade print freshness (only check if we have a trade)
        if snapshot.last_trade_ts_ns > 0 {
            let trade_age_ms =
                ((now_ns - snapshot.last_trade_ts_ns).max(0) as u64) / 1_000_000;
            if trade_age_ms > self.trade_max_ms {
                snapshot.is_stale = true;
                snapshot.stale_reason = format!(
                    "TRADE_TTL_BREACH: age={}ms max={}ms",
                    trade_age_ms, self.trade_max_ms
                );
            }
        }
    }
}

/// Enriched snapshot carrying last trade timestamp for TTL check.
/// We attach this to MarketSnapshot transiently during the hot path.
pub trait TtlAware {
    fn last_trade_ts_ns(&self) -> i64;
    fn set_stale(&mut self, reason: String);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::normalizer::MarketSnapshot;

    fn make_snap(exchange_ts_ns: i64, last_trade_ts_ns_val: i64) -> MarketSnapshot {
        MarketSnapshot {
            symbol: "TEST".into(),
            ingestion_ts_ns: 0,
            exchange_ts_ns,
            mid_price: 100.0,
            bid_price: 99.9,
            ask_price: 100.1,
            bid_size: 10.0,
            ask_size: 10.0,
            spread: 0.2,
            last_trade_price: 100.0,
            last_trade_size: 100.0,
            last_trade_ts_ns: last_trade_ts_ns_val,
            z_score: 0.0,
            mad_score: 0.0,
            z_mad_divergence: false,
            order_flow_imbalance: 0.0,
            realized_volatility: 0.0,
            adv_30d: 0.0,
            regime_label: "UNKNOWN".into(),
            regime_confidence: 0.0,
            is_stale: false,
            stale_reason: String::new(),
        }
    }

    // TTL checker needs access to last_trade_ts_ns; add it to MarketSnapshot
    impl MarketSnapshot {
        pub fn last_trade_ts_ns_for_test(&self) -> i64 {
            self.last_trade_ts_ns
        }
    }

    #[test]
    fn test_stale_l2_ttl_breach() {
        let checker = TtlChecker::new(1, 5);
        let now = 10_000_000; // 10ms in ns
        // exchange_ts_ns = 5ms ago → age = 5ms > l2_max=1ms
        let mut snap = make_snap(now - 5_000_000, now);
        checker.check(&mut snap, now);
        assert!(snap.is_stale);
        assert!(snap.stale_reason.contains("L2_TTL_BREACH"));
    }

    #[test]
    fn test_fresh_within_l2_ttl() {
        let checker = TtlChecker::new(1, 5);
        let now = 10_000_000;
        // exchange_ts_ns = 0.5ms ago → age = 0.5ms < l2_max=1ms
        let mut snap = make_snap(now - 500_000, now);
        checker.check(&mut snap, now);
        assert!(!snap.is_stale);
    }
}
