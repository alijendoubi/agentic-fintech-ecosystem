//! Control C02: payload validity. Required fields present, enums known,
//! doubles finite and in range, quantity > 0, side known, `signal_id` a UUID.
//!
//! Money is read from the `_nanos` fields only. The deprecated `double` money
//! fields are read solely when `accept_legacy_double_fields` is set and the
//! matching `_nanos` field is unset, and only through
//! [`Nanos::from_legacy_f64`], which rejects NaN/Inf/<=0/overflow.

use crate::domain::{Side, ValidatedSignal};
use crate::money::Nanos;
use crate::pb::{ControlResult, ReasonCode, RegimeLabel, SignalSide, TradeSignal};

const CONTROL_ID: &str = "C02";
const MAX_SYMBOL_LEN: usize = 12;
const MAX_STRATEGY_ID_LEN: usize = 128;
const UUID_CANONICAL_LEN: usize = 36;

fn is_unit_interval(v: f64) -> bool {
    v.is_finite() && (0.0..=1.0).contains(&v)
}

fn valid_symbol(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= MAX_SYMBOL_LEN
        && s.chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '.')
}

fn parse_side(raw: i32) -> Result<Side, String> {
    match SignalSide::try_from(raw) {
        Ok(SignalSide::Buy) => Ok(Side::Buy),
        Ok(SignalSide::Sell) => Ok(Side::Sell),
        Ok(SignalSide::SellShort) => Ok(Side::SellShort),
        Ok(SignalSide::SideUnknown) => Err("side is unset/UNKNOWN".into()),
        Err(_) => Err(format!("unknown side value {raw}")),
    }
}

#[allow(deprecated)]
fn parse_quantity(s: &TradeSignal, accept_legacy: bool) -> Result<Nanos, String> {
    if s.quantity_nanos < 0 {
        return Err("quantity_nanos is negative".into());
    }
    if s.quantity_nanos > 0 {
        return Ok(Nanos::new(s.quantity_nanos));
    }
    if !accept_legacy {
        return Err("quantity_nanos must be > 0".into());
    }
    Nanos::from_legacy_f64(s.quantity).map_err(|e| format!("legacy quantity rejected: {e}"))
}

/// `Ok(None)` = market order.
#[allow(deprecated)]
fn parse_price(s: &TradeSignal, accept_legacy: bool) -> Result<Option<Nanos>, String> {
    if s.price_limit_nanos < 0 {
        return Err("price_limit_nanos is negative".into());
    }
    if s.price_limit_nanos > 0 {
        return Ok(Some(Nanos::new(s.price_limit_nanos)));
    }
    if !accept_legacy {
        return Ok(None);
    }
    if s.price_limit.is_nan() || s.price_limit.is_infinite() || s.price_limit < 0.0 {
        return Err("legacy price_limit is not a valid price".into());
    }
    if s.price_limit == 0.0 {
        return Ok(None);
    }
    Nanos::from_legacy_f64(s.price_limit)
        .map(Some)
        .map_err(|e| format!("legacy price_limit rejected: {e}"))
}

fn validate_scalars(s: &TradeSignal, problems: &mut Vec<String>) {
    if s.signal_id.len() != UUID_CANONICAL_LEN || uuid::Uuid::parse_str(&s.signal_id).is_err() {
        problems.push("signal_id is not a canonical UUID".into());
    }
    if !valid_symbol(&s.symbol) {
        problems.push("symbol is not a valid upper-case ticker".into());
    }
    if s.created_at_ns <= 0 {
        problems.push("created_at_ns must be > 0".into());
    }
    if !is_unit_interval(s.omega) {
        problems.push("omega must be finite and in [0, 1]".into());
    }
    if !is_unit_interval(s.regime_confidence) {
        problems.push("regime_confidence must be finite and in [0, 1]".into());
    }
    if s.strategy_id.len() > MAX_STRATEGY_ID_LEN {
        problems.push("strategy_id too long".into());
    }
}

/// Validate a wire signal. On failure returns the failed C02 `ControlResult`.
pub fn validate_signal(
    s: &TradeSignal,
    accept_legacy: bool,
) -> Result<ValidatedSignal, Box<ControlResult>> {
    let mut problems = Vec::new();
    validate_scalars(s, &mut problems);
    let side = parse_side(s.side).map_err(|e| problems.push(e)).ok();
    let regime = RegimeLabel::try_from(s.regime)
        .map_err(|_| problems.push(format!("unknown regime value {}", s.regime)))
        .ok();
    let qty = parse_quantity(s, accept_legacy)
        .map_err(|e| problems.push(e))
        .ok();
    let limit_price = parse_price(s, accept_legacy)
        .map_err(|e| problems.push(e))
        .ok();
    match (side, regime, qty, limit_price, problems.is_empty()) {
        (Some(side), Some(regime), Some(qty), Some(limit_price), true) => Ok(ValidatedSignal {
            signal_id: s.signal_id.clone(),
            symbol: s.symbol.clone(),
            created_at_ns: s.created_at_ns,
            valid_until_ns: s.valid_until_ns,
            side,
            qty,
            limit_price,
            omega: s.omega,
            regime,
            regime_confidence: s.regime_confidence,
            strategy_id: s.strategy_id.clone(),
        }),
        _ => Err(Box::new(
            ControlResult::fail(
                CONTROL_ID,
                true,
                ReasonCode::ReasonInvalidSignal,
                "valid payload",
                "invalid",
            )
            .with_detail(problems.join("; ")),
        )),
    }
}

/// Result for a valid payload.
pub fn pass_result() -> ControlResult {
    ControlResult::pass(CONTROL_ID, true, "valid payload", "valid")
}

#[cfg(test)]
pub(crate) fn sample_signal() -> TradeSignal {
    TradeSignal {
        signal_id: "0b4e7c9e-6a61-4b0e-9a54-0e1e5d3f9a11".into(),
        symbol: "AAPL".into(),
        created_at_ns: 1_000_000_000_000,
        side: SignalSide::Buy as i32,
        quantity_nanos: 10_000_000_000,
        price_limit_nanos: 150_000_000_000,
        omega: 0.8,
        regime: RegimeLabel::TrendingBull as i32,
        regime_confidence: 0.9,
        valid_until_ns: 1_000_000_000_000 + 4_000_000_000,
        strategy_id: "AFE-STRATEGY-001".into(),
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reject(s: &TradeSignal, legacy: bool) -> String {
        let r = validate_signal(s, legacy).unwrap_err();
        assert!(!r.passed && r.is_hard);
        assert_eq!(r.reason, ReasonCode::ReasonInvalidSignal as i32);
        r.detail
    }

    #[test]
    fn c02_accepts_a_well_formed_signal() {
        let v = validate_signal(&sample_signal(), false).unwrap();
        assert_eq!(v.qty, Nanos::new(10_000_000_000));
        assert_eq!(v.limit_price, Some(Nanos::new(150_000_000_000)));
        assert_eq!(v.side, Side::Buy);
    }

    #[test]
    fn c02_zero_price_means_market() {
        let mut s = sample_signal();
        s.price_limit_nanos = 0;
        assert_eq!(validate_signal(&s, false).unwrap().limit_price, None);
    }

    #[test]
    fn c02_rejects_bad_ids_symbols_and_timestamps() {
        for f in [
            (|s: &mut TradeSignal| s.signal_id = String::new()) as fn(&mut TradeSignal),
            |s| s.signal_id = "not-a-uuid".into(),
            |s| s.signal_id = "0b4e7c9e6a614b0e9a540e1e5d3f9a11".into(),
            |s| s.symbol = String::new(),
            |s| s.symbol = "aapl".into(),
            |s| s.symbol = "TOOLONGSYMBOL1".into(),
            |s| s.created_at_ns = 0,
            |s| s.created_at_ns = -5,
        ] {
            let mut s = sample_signal();
            f(&mut s);
            reject(&s, false);
        }
    }

    #[test]
    fn c02_rejects_unknown_and_unset_enums() {
        let mut s = sample_signal();
        s.side = SignalSide::SideUnknown as i32;
        reject(&s, false);
        s.side = 99;
        reject(&s, false);
        let mut s = sample_signal();
        s.regime = 42;
        reject(&s, false);
        s.regime = -1;
        reject(&s, false);
    }

    #[test]
    fn c02_rejects_non_finite_and_out_of_range_doubles() {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -0.1, 1.0000001] {
            let mut s = sample_signal();
            s.omega = bad;
            reject(&s, false);
            let mut s = sample_signal();
            s.regime_confidence = bad;
            reject(&s, false);
        }
    }

    #[test]
    fn c02_rejects_zero_and_negative_quantity_and_negative_price() {
        let mut s = sample_signal();
        s.quantity_nanos = 0;
        reject(&s, false);
        s.quantity_nanos = -1;
        reject(&s, false);
        let mut s = sample_signal();
        s.price_limit_nanos = -1;
        reject(&s, false);
    }

    #[test]
    #[allow(deprecated)]
    fn c02_ignores_deprecated_doubles_unless_legacy_accepted() {
        let mut s = sample_signal();
        s.quantity_nanos = 0;
        s.quantity = 10.0;
        reject(&s, false);
        let v = validate_signal(&s, true).unwrap();
        assert_eq!(v.qty, Nanos::new(10_000_000_000));
        // legacy doubles that are NaN/Inf/negative are rejected when used
        for bad in [f64::NAN, f64::INFINITY, -3.0, 0.0] {
            let mut s = sample_signal();
            s.quantity_nanos = 0;
            s.quantity = bad;
            reject(&s, true);
        }
        let mut s = sample_signal();
        s.price_limit_nanos = 0;
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, -1.0] {
            s.price_limit = bad;
            reject(&s, true);
        }
        // a NaN legacy double is not read when the nanos field is set
        let mut s = sample_signal();
        s.quantity = f64::NAN;
        assert!(validate_signal(&s, true).is_ok());
    }

    #[test]
    fn c02_reports_every_problem() {
        let mut s = sample_signal();
        s.symbol = String::new();
        s.omega = f64::NAN;
        let detail = reject(&s, false);
        assert!(detail.contains("symbol") && detail.contains("omega"));
    }
}
