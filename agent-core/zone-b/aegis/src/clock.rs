//! Time source. Wall-clock nanoseconds since the Unix epoch. Injected so tests
//! (and the kill-switch heartbeat logic) are deterministic. A clock that cannot
//! be read yields `Err`, which callers turn into a REJECT.

use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use thiserror::Error;

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
#[error("system clock unavailable or out of range")]
pub struct ClockError;

pub trait Clock: Send + Sync {
    fn now_ns(&self) -> Result<i64, ClockError>;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ns(&self) -> Result<i64, ClockError> {
        let d = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| ClockError)?;
        i64::try_from(d.as_nanos()).map_err(|_| ClockError)
    }
}

/// Manually advanced clock for tests and deterministic simulations.
#[derive(Debug, Clone)]
pub struct ManualClock(Arc<AtomicI64>);

impl ManualClock {
    pub fn new(start_ns: i64) -> Self {
        ManualClock(Arc::new(AtomicI64::new(start_ns)))
    }

    pub fn set(&self, ns: i64) {
        self.0.store(ns, Ordering::SeqCst);
    }

    pub fn advance_ms(&self, ms: i64) {
        self.0
            .fetch_add(ms.saturating_mul(1_000_000), Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now_ns(&self) -> Result<i64, ClockError> {
        Ok(self.0.load(Ordering::SeqCst))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn system_clock_is_after_2020_and_manual_clock_advances() {
        assert!(SystemClock.now_ns().unwrap() > 1_577_836_800_000_000_000);
        let c = ManualClock::new(5);
        c.advance_ms(2);
        assert_eq!(c.now_ns().unwrap(), 2_000_005);
        c.set(9);
        assert_eq!(c.now_ns().unwrap(), 9);
    }
}
