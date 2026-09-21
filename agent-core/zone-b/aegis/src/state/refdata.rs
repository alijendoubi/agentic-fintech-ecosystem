//! Aegis-owned reference data (spec 2 "Aegis trusts nothing from Zone A except
//! the request"): last market snapshot per symbol and the latest regime label,
//! fed by Zone B subscriptions, NEVER by the signal.
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
    fn regime(&self) -> Option<RegimeView>;
    /// True if at least one symbol has a non-stale price no older than `max_age_ns`.
    fn has_fresh_data(&self, now_ns: i64, max_age_ns: i64) -> bool;
}

#[derive(Debug, Default)]
pub struct MemoryReferenceData {
    prices: Mutex<HashMap<String, RefPrice>>,
    regime: Mutex<Option<RegimeView>>,
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
        };
        assert!(d.regime().is_none());
        d.update_regime(&ok).unwrap();
        assert_eq!(d.regime().unwrap().label, pb::RegimeLabel::TrendingBull);
        for bad in [
            pb::RegimeLabelPacket {
                confidence: f64::NAN,
                ..ok
            },
            pb::RegimeLabelPacket {
                confidence: 1.5,
                ..ok
            },
            pb::RegimeLabelPacket { label: 99, ..ok },
            pb::RegimeLabelPacket {
                timestamp_ns: 0,
                ..ok
            },
        ] {
            assert!(d.update_regime(&bad).is_err());
        }
    }
}
