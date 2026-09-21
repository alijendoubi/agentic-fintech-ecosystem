//! Token buckets for control C16 (global and per symbol). Pure integer math:
//! tokens are scaled by 1e9 so refill is exact (`elapsed_ns * refill_per_sec`).
//! A clock that goes backwards adds no tokens.

use std::collections::HashMap;

use crate::controls::RateView;
use crate::limits::RateLimit;

const SCALE: i128 = 1_000_000_000;
/// Cap on tracked symbols (bounded memory; unknown symbols are rejected by C03
/// before they matter, but the engine also gates on this).
const MAX_SYMBOLS: usize = 4_096;

#[derive(Debug, Clone)]
pub struct TokenBucket {
    capacity: i128,
    tokens: i128,
    refill_per_sec: i128,
    last_ns: i64,
}

impl TokenBucket {
    pub fn new(limit: RateLimit, now_ns: i64) -> TokenBucket {
        let capacity = i128::from(limit.capacity) * SCALE;
        TokenBucket {
            capacity,
            tokens: capacity,
            refill_per_sec: i128::from(limit.refill_per_sec),
            last_ns: now_ns,
        }
    }

    fn refill(&mut self, now_ns: i64) {
        let elapsed = i128::from(now_ns.saturating_sub(self.last_ns)).max(0);
        self.tokens = (self.tokens + elapsed * self.refill_per_sec).min(self.capacity);
        if now_ns > self.last_ns {
            self.last_ns = now_ns;
        }
    }

    pub fn has_token(&mut self, now_ns: i64) -> bool {
        self.refill(now_ns);
        self.tokens >= SCALE
    }

    fn take(&mut self) {
        self.tokens -= SCALE;
    }
}

#[derive(Debug)]
pub struct RateLimiter {
    global: TokenBucket,
    per_symbol_limit: RateLimit,
    symbols: HashMap<String, TokenBucket>,
}

impl RateLimiter {
    pub fn new(global: RateLimit, per_symbol: RateLimit, now_ns: i64) -> RateLimiter {
        RateLimiter {
            global: TokenBucket::new(global, now_ns),
            per_symbol_limit: per_symbol,
            symbols: HashMap::new(),
        }
    }

    /// Consume one token from BOTH buckets iff both have one; a rejection
    /// consumes nothing.
    pub fn check_and_take(&mut self, symbol: &str, now_ns: i64) -> RateView {
        if !self.symbols.contains_key(symbol) && self.symbols.len() >= MAX_SYMBOLS {
            return RateView {
                global_ok: true,
                symbol_ok: false,
            };
        }
        let limit = self.per_symbol_limit;
        let bucket = self
            .symbols
            .entry(symbol.to_owned())
            .or_insert_with(|| TokenBucket::new(limit, now_ns));
        let global_ok = self.global.has_token(now_ns);
        let symbol_ok = bucket.has_token(now_ns);
        if global_ok && symbol_ok {
            self.global.take();
            bucket.take();
        }
        RateView {
            global_ok,
            symbol_ok,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SEC: i64 = 1_000_000_000;

    fn lim(capacity: u32, refill: u32) -> RateLimit {
        RateLimit {
            capacity,
            refill_per_sec: refill,
        }
    }

    #[test]
    fn burst_then_exhaustion_then_exact_refill() {
        let mut r = RateLimiter::new(lim(100, 100), lim(3, 1), 0);
        for _ in 0..3 {
            assert!(r.check_and_take("AAPL", 0).symbol_ok);
        }
        let v = r.check_and_take("AAPL", 0);
        assert!(v.global_ok && !v.symbol_ok);
        // half a second: 0.5 token, still empty; one second: one token
        assert!(!r.check_and_take("AAPL", SEC / 2).symbol_ok);
        assert!(r.check_and_take("AAPL", SEC).symbol_ok);
        assert!(!r.check_and_take("AAPL", SEC).symbol_ok);
    }

    #[test]
    fn buckets_are_per_symbol_and_global_is_shared() {
        let mut r = RateLimiter::new(lim(2, 1), lim(5, 5), 0);
        assert!(r.check_and_take("AAPL", 0).global_ok);
        assert!(r.check_and_take("MSFT", 0).symbol_ok);
        let v = r.check_and_take("AAPL", 0);
        assert!(!v.global_ok && v.symbol_ok);
    }

    #[test]
    fn a_rejection_consumes_nothing_and_time_going_backwards_adds_nothing() {
        let mut r = RateLimiter::new(lim(1, 1), lim(1, 1), 10 * SEC);
        assert!(r.check_and_take("AAPL", 10 * SEC).symbol_ok);
        assert!(!r.check_and_take("AAPL", 5 * SEC).symbol_ok);
        assert!(!r.check_and_take("AAPL", 10 * SEC).symbol_ok);
        assert!(r.check_and_take("AAPL", 11 * SEC).symbol_ok);
    }

    #[test]
    fn refill_never_exceeds_capacity() {
        let mut r = RateLimiter::new(lim(2, 1_000), lim(2, 1_000), 0);
        assert!(r.check_and_take("A", 1_000 * SEC).symbol_ok);
        assert!(r.check_and_take("A", 1_000 * SEC).symbol_ok);
        assert!(!r.check_and_take("A", 1_000 * SEC).symbol_ok);
    }
}
