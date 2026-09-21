//! Validated, typed view of a `TradeSignal`. Nothing downstream of
//! `validate::validate_signal` ever sees a raw wire message.

use crate::money::Nanos;
use crate::pb::RegimeLabel;

/// Trade direction as the signal states it (`SELL_SHORT` is kept distinct;
/// see README "Attestation contract" for the mapping to `OrderSide`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Buy,
    Sell,
    SellShort,
}

impl Side {
    /// True for the sell-like sides.
    pub fn is_sell(self) -> bool {
        matches!(self, Side::Sell | Side::SellShort)
    }
}

/// Whether the evaluation is a fresh submission or the release of a held
/// signal by a human (spec 4.6: hard controls re-run, soft controls are what
/// the human is deciding, and the signal id is already known).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Submit,
    Release,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ValidatedSignal {
    pub signal_id: String,
    pub symbol: String,
    pub created_at_ns: i64,
    pub valid_until_ns: i64,
    pub side: Side,
    pub qty: Nanos,
    /// `None` = market order (Aegis converts to a bounded marketable limit).
    pub limit_price: Option<Nanos>,
    pub omega: f64,
    pub regime: RegimeLabel,
    pub regime_confidence: f64,
    pub strategy_id: String,
}
