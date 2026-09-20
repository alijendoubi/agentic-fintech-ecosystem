//! Process-wide counters (lock-free) with a periodic structured summary, so
//! silent drops are visible in logs instead of being swallowed.

use std::sync::atomic::{AtomicU64, Ordering};

use tracing::info;

#[derive(Debug, Default)]
pub struct Metrics {
    pub frames: AtomicU64,
    pub bad_frames: AtomicU64,
    pub quotes: AtomicU64,
    pub trades: AtomicU64,
    pub rejected_messages: AtomicU64,
    pub snapshots: AtomicU64,
    pub stale_snapshots: AtomicU64,
    pub reconnects: AtomicU64,
    pub ilp_written: AtomicU64,
    pub ilp_invalid: AtomicU64,
    pub ilp_write_errors: AtomicU64,
    pub redis_published: AtomicU64,
    pub redis_errors: AtomicU64,
    pub regime_updates: AtomicU64,
    pub regime_rejected: AtomicU64,
}

impl Metrics {
    pub fn inc(counter: &AtomicU64) {
        counter.fetch_add(1, Ordering::Relaxed);
    }

    pub fn add(counter: &AtomicU64, n: u64) {
        counter.fetch_add(n, Ordering::Relaxed);
    }

    pub fn get(counter: &AtomicU64) -> u64 {
        counter.load(Ordering::Relaxed)
    }

    /// Emit one structured log line with every counter.
    pub fn log_summary(&self, ilp_dropped: u64, redis_dropped: u64) {
        let g = Self::get;
        info!(
            frames = g(&self.frames),
            bad_frames = g(&self.bad_frames),
            quotes = g(&self.quotes),
            trades = g(&self.trades),
            rejected_messages = g(&self.rejected_messages),
            snapshots = g(&self.snapshots),
            stale_snapshots = g(&self.stale_snapshots),
            reconnects = g(&self.reconnects),
            ilp_written = g(&self.ilp_written),
            ilp_invalid = g(&self.ilp_invalid),
            ilp_write_errors = g(&self.ilp_write_errors),
            ilp_dropped,
            redis_published = g(&self.redis_published),
            redis_errors = g(&self.redis_errors),
            redis_dropped,
            regime_updates = g(&self.regime_updates),
            regime_rejected = g(&self.regime_rejected),
            "metrics"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counters_increment() {
        let m = Metrics::default();
        Metrics::inc(&m.quotes);
        Metrics::add(&m.quotes, 4);
        assert_eq!(Metrics::get(&m.quotes), 5);
        m.log_summary(0, 0); // must not panic without a subscriber
    }
}
