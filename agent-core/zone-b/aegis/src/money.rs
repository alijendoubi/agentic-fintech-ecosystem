//! Fixed-point money (spec section 3): int64 "nanos" (1e-9 units), no floats on
//! the decision path. Products are computed in i128 with checked operations and
//! rounded conservatively (notional and exposure round UP).
//!
//! The only float entry point is [`Nanos::from_legacy_f64`], used at the wire
//! boundary for deprecated double fields; it rejects NaN/Inf/<=0/overflow.

use std::fmt;

use thiserror::Error;

/// Nanos per whole unit (1 share, 1 USD).
pub const NANOS_PER_UNIT: i128 = 1_000_000_000;

#[derive(Debug, Error, PartialEq, Eq, Clone, Copy)]
pub enum MoneyError {
    #[error("arithmetic overflow")]
    Overflow,
    #[error("value is not finite")]
    NotFinite,
    #[error("value must be strictly positive")]
    NotPositive,
}

/// A fixed-point amount in units of 1e-9. May be negative (positions).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Nanos(i64);

impl Nanos {
    pub const ZERO: Nanos = Nanos(0);

    pub const fn new(raw: i64) -> Self {
        Nanos(raw)
    }

    pub const fn get(self) -> i64 {
        self.0
    }

    pub fn checked_add(self, other: Nanos) -> Result<Nanos, MoneyError> {
        self.0
            .checked_add(other.0)
            .map(Nanos)
            .ok_or(MoneyError::Overflow)
    }

    pub fn checked_sub(self, other: Nanos) -> Result<Nanos, MoneyError> {
        self.0
            .checked_sub(other.0)
            .map(Nanos)
            .ok_or(MoneyError::Overflow)
    }

    /// Absolute value as i128 (cannot overflow, unlike `i64::abs`).
    pub fn abs_wide(self) -> i128 {
        i128::from(self.0).abs()
    }

    /// Convert a deprecated wire `double` at the boundary only. Rejects
    /// NaN/Inf, values <= 0 and values that do not fit; rounds half-up to the
    /// nearest nano.
    pub fn from_legacy_f64(value: f64) -> Result<Nanos, MoneyError> {
        if !value.is_finite() {
            return Err(MoneyError::NotFinite);
        }
        if value <= 0.0 {
            return Err(MoneyError::NotPositive);
        }
        let scaled = (value * 1e9).round();
        // 2^63 as f64 is exactly representable; anything at or above overflows.
        // `scaled` is finite here (checked above), so `>=` is total.
        if scaled >= 9_223_372_036_854_775_808.0 {
            return Err(MoneyError::Overflow);
        }
        // Truncation is exact: `scaled` is a non-negative integer-valued float
        // strictly below 2^63.
        Ok(Nanos(scaled as i64))
    }
}

impl fmt::Display for Nanos {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&format_wide(i128::from(self.0)))
    }
}

/// Decimal string of an i128 nanos amount, e.g. `-12.500000000`.
pub fn format_wide(value: i128) -> String {
    let sign = if value < 0 { "-" } else { "" };
    let abs = value.unsigned_abs();
    let unit = NANOS_PER_UNIT as u128;
    format!("{sign}{}.{:09}", abs / unit, abs % unit)
}

/// `ceil(a * b / 1e9)` for non-negative operands (price x quantity -> notional).
pub fn mul_up(a: i128, b: i128) -> Result<i128, MoneyError> {
    if a < 0 || b < 0 {
        return Err(MoneyError::NotPositive);
    }
    let product = a.checked_mul(b).ok_or(MoneyError::Overflow)?;
    let bumped = product
        .checked_add(NANOS_PER_UNIT - 1)
        .ok_or(MoneyError::Overflow)?;
    Ok(bumped / NANOS_PER_UNIT)
}

/// `ceil(value * bps / 10_000)` for non-negative `value` (conservative up).
pub fn bps_of_up(value: i128, bps: u32) -> Result<i128, MoneyError> {
    if value < 0 {
        return Err(MoneyError::NotPositive);
    }
    let product = value
        .checked_mul(i128::from(bps))
        .ok_or(MoneyError::Overflow)?;
    let bumped = product.checked_add(9_999).ok_or(MoneyError::Overflow)?;
    Ok(bumped / 10_000)
}

/// `floor(value * bps / 10_000)` for non-negative `value` (headroom rounds down).
pub fn bps_of_down(value: i128, bps: u32) -> Result<i128, MoneyError> {
    if value < 0 {
        return Err(MoneyError::NotPositive);
    }
    let product = value
        .checked_mul(i128::from(bps))
        .ok_or(MoneyError::Overflow)?;
    Ok(product / 10_000)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mul_up_exact_and_rounds_up() {
        // 100.5 x 2 = 201.0 exactly
        assert_eq!(
            mul_up(100_500_000_000, 2_000_000_000).unwrap(),
            201_000_000_000
        );
        // 1 nano x 1 nano = 1e-18 -> rounds UP to 1 nano
        assert_eq!(mul_up(1, 1).unwrap(), 1);
        assert_eq!(mul_up(0, 5).unwrap(), 0);
    }

    #[test]
    fn mul_up_rejects_negative_and_overflow() {
        assert_eq!(mul_up(-1, 1), Err(MoneyError::NotPositive));
        assert_eq!(mul_up(1, -1), Err(MoneyError::NotPositive));
        assert_eq!(mul_up(i128::MAX, 2), Err(MoneyError::Overflow));
        assert_eq!(
            mul_up(i128::MAX / 2, i128::MAX / 2),
            Err(MoneyError::Overflow)
        );
    }

    #[test]
    fn bps_rounding_directions() {
        // 1 bp of 1 nano: up -> 1, down -> 0
        assert_eq!(bps_of_up(1, 1).unwrap(), 1);
        assert_eq!(bps_of_down(1, 1).unwrap(), 0);
        assert_eq!(bps_of_up(10_000, 150).unwrap(), 150);
        assert_eq!(bps_of_down(10_000, 150).unwrap(), 150);
        assert_eq!(bps_of_up(i128::MAX, 2), Err(MoneyError::Overflow));
        assert_eq!(bps_of_down(-1, 1), Err(MoneyError::NotPositive));
    }

    #[test]
    fn legacy_double_boundary() {
        assert_eq!(
            Nanos::from_legacy_f64(1.5).unwrap(),
            Nanos::new(1_500_000_000)
        );
        // rounds half-up at the nano
        assert_eq!(
            Nanos::from_legacy_f64(0.000_000_000_5).unwrap(),
            Nanos::new(1)
        );
        assert_eq!(Nanos::from_legacy_f64(f64::NAN), Err(MoneyError::NotFinite));
        assert_eq!(
            Nanos::from_legacy_f64(f64::INFINITY),
            Err(MoneyError::NotFinite)
        );
        assert_eq!(
            Nanos::from_legacy_f64(f64::NEG_INFINITY),
            Err(MoneyError::NotFinite)
        );
        assert_eq!(Nanos::from_legacy_f64(0.0), Err(MoneyError::NotPositive));
        assert_eq!(Nanos::from_legacy_f64(-0.0), Err(MoneyError::NotPositive));
        assert_eq!(Nanos::from_legacy_f64(-1.0), Err(MoneyError::NotPositive));
        assert_eq!(Nanos::from_legacy_f64(1e30), Err(MoneyError::Overflow));
        assert_eq!(Nanos::from_legacy_f64(9.3e9), Err(MoneyError::Overflow));
    }

    #[test]
    fn checked_ops_detect_overflow() {
        assert_eq!(
            Nanos::new(i64::MAX).checked_add(Nanos::new(1)),
            Err(MoneyError::Overflow)
        );
        assert_eq!(
            Nanos::new(i64::MIN).checked_sub(Nanos::new(1)),
            Err(MoneyError::Overflow)
        );
        assert_eq!(
            Nanos::new(i64::MIN).abs_wide(),
            9_223_372_036_854_775_808_i128
        );
    }

    #[test]
    fn decimal_formatting() {
        assert_eq!(format_wide(-12_500_000_000), "-12.500000000");
        assert_eq!(format_wide(1), "0.000000001");
        assert_eq!(Nanos::new(0).to_string(), "0.000000000");
    }
}
