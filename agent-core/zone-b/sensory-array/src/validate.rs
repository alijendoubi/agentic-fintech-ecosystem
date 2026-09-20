//! Boundary validation shared by every module that touches feed data.
//!
//! Everything arriving from the WebSocket or Redis is untrusted: tickers end
//! up as ILP tags and map keys, numbers end up in arithmetic and text
//! protocols. Anything that does not pass here is rejected (and counted),
//! never repaired.

use std::collections::HashSet;

/// Upper bound on distinct symbols tracked by any per-symbol map.
pub const MAX_SYMBOLS: usize = 20_000;
/// Longest accepted ticker.
pub const MAX_TICKER_LEN: usize = 16;
/// Sanity ceiling for a US-equity price (USD).
pub const MAX_PRICE: f64 = 1.0e7;
/// Sanity ceiling for a quote/trade size (shares).
pub const MAX_SIZE: f64 = 1.0e12;

/// Strict ticker grammar: `[A-Z0-9][A-Z0-9._-]{0,15}`.
pub fn is_valid_ticker(s: &str) -> bool {
    let b = s.as_bytes();
    if b.is_empty() || b.len() > MAX_TICKER_LEN || !b[0].is_ascii_alphanumeric() {
        return false;
    }
    b.iter()
        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || matches!(c, b'.' | b'-' | b'_'))
}

pub fn is_valid_price(p: f64) -> bool {
    p.is_finite() && p > 0.0 && p <= MAX_PRICE
}

pub fn is_valid_size(s: f64) -> bool {
    s.is_finite() && (0.0..=MAX_SIZE).contains(&s)
}

/// Milliseconds -> nanoseconds; `None` for non-positive or overflowing input.
pub fn ms_to_ns(ms: i64) -> Option<i64> {
    if ms <= 0 {
        return None;
    }
    ms.checked_mul(1_000_000)
}

/// Which symbols are accepted from the feed / from Zone A.
#[derive(Debug, Clone)]
pub enum SymbolFilter {
    /// Wildcard subscription (`*`): any *valid* ticker.
    Any,
    /// Only the configured subscription set.
    Only(HashSet<String>),
}

impl SymbolFilter {
    pub fn from_symbols(symbols: &[String]) -> Self {
        if symbols.iter().any(|s| s == "*") {
            SymbolFilter::Any
        } else {
            SymbolFilter::Only(symbols.iter().cloned().collect())
        }
    }

    /// Valid ticker grammar AND member of the configured set (if any).
    pub fn allows(&self, symbol: &str) -> bool {
        if !is_valid_ticker(symbol) {
            return false;
        }
        match self {
            SymbolFilter::Any => true,
            SymbolFilter::Only(set) => set.contains(symbol),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_normal_tickers() {
        for t in ["AAPL", "BRK.B", "BF-B", "A", "X_1"] {
            assert!(is_valid_ticker(t), "{t}");
        }
    }

    #[test]
    fn rejects_hostile_and_malformed_tickers() {
        let long = "A".repeat(MAX_TICKER_LEN + 1);
        for t in [
            "",
            " ",
            "aapl",
            "AA PL",
            "AAPL,x=1",
            "AAPL\nmarket_data,symbol=X",
            "AAPL\r",
            "A=B",
            "A\\B",
            ".AAPL",
            "AAPL\"",
            "\u{0410}APL", // Cyrillic A
            long.as_str(),
        ] {
            assert!(!is_valid_ticker(t), "{t:?}");
        }
    }

    #[test]
    fn price_and_size_bounds() {
        assert!(is_valid_price(150.25));
        for p in [0.0, -1.0, f64::NAN, f64::INFINITY, 1e308, MAX_PRICE * 2.0] {
            assert!(!is_valid_price(p), "{p}");
        }
        assert!(is_valid_size(0.0));
        for s in [-1.0, f64::NAN, f64::NEG_INFINITY, 1e300] {
            assert!(!is_valid_size(s), "{s}");
        }
    }

    #[test]
    fn ms_to_ns_rejects_overflow_and_non_positive() {
        assert_eq!(ms_to_ns(1_700_000_000_000), Some(1_700_000_000_000_000_000));
        assert_eq!(ms_to_ns(0), None);
        assert_eq!(ms_to_ns(-5), None);
        assert_eq!(ms_to_ns(i64::MAX), None);
        assert_eq!(ms_to_ns(9_223_372_036_855), None); // just past i64::MAX / 1e6
    }

    #[test]
    fn symbol_filter_only_and_any() {
        let only = SymbolFilter::from_symbols(&["AAPL".into(), "MSFT".into()]);
        assert!(only.allows("AAPL"));
        assert!(!only.allows("TSLA"));
        assert!(!only.allows("AAPL\n"));
        let any = SymbolFilter::from_symbols(&["*".into()]);
        assert!(any.allows("TSLA"));
        assert!(!any.allows("tsla"));
    }
}
