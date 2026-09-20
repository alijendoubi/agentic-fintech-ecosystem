//! Held signals awaiting a human (spec 4.6). In memory by design: a restart
//! loses the holds, which is fail closed (an unresolved hold expires as a
//! REJECT anyway). Bounded.

use std::collections::HashMap;

use crate::domain::ValidatedSignal;
use crate::pb;

pub const MAX_HOLDS: usize = 1_000;

#[derive(Debug, Clone)]
pub struct Held {
    pub hold_id: String,
    pub signal: pb::TradeSignal,
    pub validated: ValidatedSignal,
    pub payload_sha256: [u8; 32],
    pub expires_at_ns: i64,
}

#[derive(Debug, Default)]
pub struct HoldStore {
    map: HashMap<String, Held>,
}

impl HoldStore {
    /// `false` when full (the caller then rejects instead of holding).
    pub fn insert(&mut self, held: Held) -> bool {
        if self.map.len() >= MAX_HOLDS {
            return false;
        }
        self.map.insert(held.hold_id.clone(), held);
        true
    }

    pub fn get(&self, hold_id: &str) -> Option<&Held> {
        self.map.get(hold_id)
    }

    pub fn take(&mut self, hold_id: &str) -> Option<Held> {
        self.map.remove(hold_id)
    }

    /// Remove and return every hold that expired before `now_ns`.
    pub fn take_expired(&mut self, now_ns: i64) -> Vec<Held> {
        let expired: Vec<String> = self
            .map
            .values()
            .filter(|h| h.expires_at_ns < now_ns)
            .map(|h| h.hold_id.clone())
            .collect();
        expired
            .iter()
            .filter_map(|id| self.map.remove(id))
            .collect()
    }

    pub fn len(&self) -> usize {
        self.map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.map.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Side;
    use crate::money::Nanos;

    fn held(id: &str, exp: i64) -> Held {
        Held {
            hold_id: id.into(),
            signal: pb::TradeSignal::default(),
            validated: ValidatedSignal {
                signal_id: "s".into(),
                symbol: "AAPL".into(),
                created_at_ns: 1,
                valid_until_ns: 2,
                side: Side::Buy,
                qty: Nanos::new(1),
                limit_price: None,
                omega: 0.9,
                regime: pb::RegimeLabel::TrendingBull,
                regime_confidence: 0.9,
                strategy_id: String::new(),
            },
            payload_sha256: [0; 32],
            expires_at_ns: exp,
        }
    }

    #[test]
    fn insert_take_expire_and_bound() {
        let mut s = HoldStore::default();
        assert!(s.insert(held("a", 10)) && s.insert(held("b", 20)));
        assert_eq!(s.len(), 2);
        assert!(s.get("a").is_some());
        let exp = s.take_expired(11);
        assert_eq!(exp.len(), 1);
        assert_eq!(exp[0].hold_id, "a");
        assert!(s.take("b").is_some() && s.take("b").is_none() && s.is_empty());
        for i in 0..MAX_HOLDS {
            assert!(s.insert(held(&format!("h{i}"), 100)));
        }
        assert!(!s.insert(held("overflow", 100)));
    }
}
