//! Aegis-owned reference data (spec 2 "Aegis trusts nothing from Zone A except
//! the request"): last market snapshot per symbol and the latest regime label
//! per symbol (ALI-158), plus a legacy universe-wide label used only for a
//! symbol that has no per-symbol label. Fed by Zone B subscriptions, NEVER by
//! the signal.
//!
//! `MarketSnapshot` still carries `double` prices; conversion to nanos happens
//! here, at the boundary, and rejects NaN/Inf/<=0. The feed is the
//! `PushReferenceData` RPC (`state::ingest`), which writes through the
//! monotonic setters below. Until something feeds this store every signal
//! fails C08 (`REASON_STALE_REFERENCE_PRICE`) and is rejected, which is the
//! safe default.

use std::collections::HashMap;
use std::sync::Mutex;

use thiserror::Error;

use crate::controls::{RefPrice, RegimeView};
use crate::money::Nanos;
use crate::pb;

/// Bound on tracked symbols.
const MAX_SYMBOLS: usize = 4_096;

#[derive(Debug, Error, PartialEq, Eq)]
pub enum RefDataError {
    #[error("invalid reference data: {0}")]
    Invalid(&'static str),
    /// The update is not strictly newer than what is stored.
    #[error("reference data is not newer than the stored value")]
    OutOfOrder,
}

pub trait ReferenceData: Send + Sync {
    fn price(&self, symbol: &str) -> Option<RefPrice>;
    /// The universe-wide (legacy) regime label.
    fn regime(&self) -> Option<RegimeView>;
    /// The regime label C18 uses for `symbol`: its own per-symbol label if one
    /// was ever stored, else the universe-wide label. Never another symbol's.
    fn regime_for(&self, symbol: &str) -> Option<RegimeView>;
    /// True if at least one symbol has a non-stale price no older than `max_age_ns`.
    fn has_fresh_data(&self, now_ns: i64, max_age_ns: i64) -> bool;
}

#[derive(Debug, Default)]
pub struct MemoryReferenceData {
    prices: Mutex<HashMap<String, RefPrice>>,
    regime: Mutex<Option<RegimeView>>,
    symbol_regimes: Mutex<HashMap<String, RegimeView>>,
}

impl MemoryReferenceData {
    pub fn set_price(&self, symbol: &str, price: RefPrice) -> Result<(), RefDataError> {
        let mut m = self
            .prices
            .lock()
            .map_err(|_| RefDataError::Invalid("lock poisoned"))?;
        if !m.contains_key(symbol) && m.len() >= MAX_SYMBOLS {
            return Err(RefDataError::Invalid("too many symbols"));
        }
        m.insert(symbol.to_owned(), price);
        Ok(())
    }

    /// Store a price only if it is strictly newer than the stored one for the
    /// symbol (compare-and-set under the lock). Older or equal => `OutOfOrder`.
    pub fn set_price_monotonic(&self, symbol: &str, price: RefPrice) -> Result<(), RefDataError> {
        let mut m = self
            .prices
            .lock()
            .map_err(|_| RefDataError::Invalid("lock poisoned"))?;
        match m.get(symbol) {
            Some(old) if old.ingested_at_ns >= price.ingested_at_ns => {
                return Err(RefDataError::OutOfOrder)
            }
            None if m.len() >= MAX_SYMBOLS => {
                return Err(RefDataError::Invalid("too many symbols"))
            }
            _ => {}
        }
        m.insert(symbol.to_owned(), price);
        Ok(())
    }

    /// Store the regime only if strictly newer than the stored one.
    pub fn set_regime_monotonic(&self, regime: RegimeView) -> Result<(), RefDataError> {
        let mut r = self
            .regime
            .lock()
            .map_err(|_| RefDataError::Invalid("lock poisoned"))?;
        if r.is_some_and(|old| old.at_ns >= regime.at_ns) {
            return Err(RefDataError::OutOfOrder);
        }
        *r = Some(regime);
        Ok(())
    }

    /// Store a per-symbol regime only if strictly newer than the stored one for
    /// that symbol (compare-and-set under the lock). Bounded like prices.
    pub fn set_symbol_regime_monotonic(
        &self,
        symbol: &str,
        regime: RegimeView,
    ) -> Result<(), RefDataError> {
        let mut m = self
            .symbol_regimes
            .lock()
            .map_err(|_| RefDataError::Invalid("lock poisoned"))?;
        match m.get(symbol) {
            Some(old) if old.at_ns >= regime.at_ns => return Err(RefDataError::OutOfOrder),
            None if m.len() >= MAX_SYMBOLS => {
                return Err(RefDataError::Invalid("too many symbols"))
            }
            _ => {}
        }
        m.insert(symbol.to_owned(), regime);
        Ok(())
    }

    pub fn set_regime(&self, regime: RegimeView) -> Result<(), RefDataError> {
        *self
            .regime
            .lock()
            .map_err(|_| RefDataError::Invalid("lock poisoned"))? = Some(regime);
        Ok(())
    }

    /// Convert a wire snapshot (doubles) at the boundary and store it.
    pub fn update_snapshot(&self, s: &pb::MarketSnapshot) -> Result<(), RefDataError> {
        let mid =
            Nanos::from_legacy_f64(s.mid_price).map_err(|_| RefDataError::Invalid("mid_price"))?;
        let adv =
            Nanos::from_legacy_f64(s.adv_30d).map_err(|_| RefDataError::Invalid("adv_30d"))?;
        if s.symbol.is_empty() || s.ingestion_timestamp_ns <= 0 {
            return Err(RefDataError::Invalid("symbol or timestamp"));
        }
        self.set_price(
            &s.symbol,
            RefPrice {
                mid,
                adv,
                ingested_at_ns: s.ingestion_timestamp_ns,
                is_stale: s.is_stale,
            },
        )
    }

    pub fn update_regime(&self, p: &pb::RegimeLabelPacket) -> Result<(), RefDataError> {
        let label = pb::RegimeLabel::try_from(p.label)
            .map_err(|_| RefDataError::Invalid("regime label"))?;
        if !p.confidence.is_finite() || !(0.0..=1.0).contains(&p.confidence) || p.timestamp_ns <= 0
        {
            return Err(RefDataError::Invalid("regime confidence or timestamp"));
        }
        self.set_regime(RegimeView {
            label,
            confidence: p.confidence,
            at_ns: p.timestamp_ns,
        })
    }
}

impl ReferenceData for MemoryReferenceData {
    fn price(&self, symbol: &str) -> Option<RefPrice> {
        self.prices.lock().ok()?.get(symbol).copied()
    }

    fn regime(&self) -> Option<RegimeView> {
        *self.regime.lock().ok()?
    }

    fn regime_for(&self, symbol: &str) -> Option<RegimeView> {
        // A poisoned per-symbol map yields None (C18 then fails closed), never
        // the universe-wide fallback.
        let own = self.symbol_regimes.lock().ok()?.get(symbol).copied();
        own.or_else(|| self.regime())
    }

    fn has_fresh_data(&self, now_ns: i64, max_age_ns: i64) -> bool {
        self.prices
            .lock()
            .map(|m| {
                m.values()
                    .any(|p| !p.is_stale && now_ns.saturating_sub(p.ingested_at_ns) <= max_age_ns)
            })
            .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn snap(mid: f64, adv: f64) -> pb::MarketSnapshot {
        pb::MarketSnapshot {
            symbol: "AAPL".into(),
            ingestion_timestamp_ns: 5,
            mid_price: mid,
            adv_30d: adv,
            ..pb::MarketSnapshot::default()
        }
    }

    #[test]
    fn snapshot_conversion_rejects_bad_doubles() {
        let d = MemoryReferenceData::default();
        assert!(d.update_snapshot(&snap(150.25, 1e6)).is_ok());
        let p = d.price("AAPL").unwrap();
        assert_eq!(p.mid, Nanos::new(150_250_000_000));
        assert_eq!(p.adv, Nanos::new(1_000_000_000_000_000));
        for (m, a) in [
            (f64::NAN, 1.0),
            (0.0, 1.0),
            (-1.0, 1.0),
            (1.0, f64::INFINITY),
            (1.0, 0.0),
        ] {
            assert!(d.update_snapshot(&snap(m, a)).is_err(), "{m} {a}");
        }
        assert!(d.price("MSFT").is_none());
        assert!(d.has_fresh_data(5, 1));
    }

    #[test]
    fn regime_update_validates() {
        let d = MemoryReferenceData::default();
        let ok = pb::RegimeLabelPacket {
            label: pb::RegimeLabel::TrendingBull as i32,
            confidence: 0.8,
            timestamp_ns: 9,
            state_index: 0,
            symbol: String::new(),
        };
        assert!(d.regime().is_none());
        d.update_regime(&ok).unwrap();
        assert_eq!(d.regime().unwrap().label, pb::RegimeLabel::TrendingBull);
        for bad in [
            pb::RegimeLabelPacket {
                confidence: f64::NAN,
                ..ok.clone()
            },
            pb::RegimeLabelPacket {
                confidence: 1.5,
                ..ok.clone()
            },
            pb::RegimeLabelPacket {
                label: 99,
                ..ok.clone()
            },
            pb::RegimeLabelPacket {
                timestamp_ns: 0,
                ..ok.clone()
            },
        ] {
            assert!(d.update_regime(&bad).is_err());
        }
    }

    fn view(label: pb::RegimeLabel, at_ns: i64) -> RegimeView {
        RegimeView {
            label,
            confidence: 0.9,
            at_ns,
        }
    }

    #[test]
    fn regime_for_uses_the_symbols_own_label_never_another_symbols() {
        let d = MemoryReferenceData::default();
        assert!(
            d.regime_for("AAPL").is_none(),
            "nothing stored: C18 fails closed"
        );
        d.set_symbol_regime_monotonic("AAPL", view(pb::RegimeLabel::TrendingBull, 10))
            .unwrap();
        d.set_symbol_regime_monotonic("MSFT", view(pb::RegimeLabel::Crisis, 20))
            .unwrap();
        assert_eq!(
            d.regime_for("AAPL").unwrap().label,
            pb::RegimeLabel::TrendingBull,
            "the newer MSFT label must not leak onto AAPL"
        );
        assert_eq!(d.regime_for("MSFT").unwrap().label, pb::RegimeLabel::Crisis);
        assert!(
            d.regime_for("NVDA").is_none(),
            "no per-symbol and no global label"
        );
    }

    #[test]
    fn universe_wide_label_is_only_a_fallback() {
        let d = MemoryReferenceData::default();
        d.set_regime(view(pb::RegimeLabel::LowVolChop, 30)).unwrap();
        assert_eq!(
            d.regime_for("NVDA").unwrap().label,
            pb::RegimeLabel::LowVolChop
        );
        d.set_symbol_regime_monotonic("NVDA", view(pb::RegimeLabel::HighVolChop, 5))
            .unwrap();
        assert_eq!(
            d.regime_for("NVDA").unwrap().label,
            pb::RegimeLabel::HighVolChop,
            "own label wins even when the global one is newer"
        );
    }

    #[test]
    fn per_symbol_regime_is_monotonic_per_symbol() {
        let d = MemoryReferenceData::default();
        d.set_symbol_regime_monotonic("AAPL", view(pb::RegimeLabel::TrendingBull, 10))
            .unwrap();
        assert_eq!(
            d.set_symbol_regime_monotonic("AAPL", view(pb::RegimeLabel::Crisis, 10)),
            Err(RefDataError::OutOfOrder)
        );
        d.set_symbol_regime_monotonic("MSFT", view(pb::RegimeLabel::Crisis, 1))
            .unwrap();
    }
}
