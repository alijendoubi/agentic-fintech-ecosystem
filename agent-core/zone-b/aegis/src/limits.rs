//! Owner-supplied risk limits (spec section 4.1 note: Aegis refuses to start if
//! any limit lacks an explicit value).
//!
//! Every risk limit is a REQUIRED field with NO default: a missing field is a
//! parse error and the service does not start. Only PROPOSED timing values that
//! merely tighten safety carry defaults (see [`Timings`]); each is marked
//! TODO(owner) in the README.
//!
//! The limits file is JSON. Its SHA-256 (over the raw bytes) is exposed through
//! `GetAegisState` and embedded in every attestation.

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::error::ConfigError;
use crate::hex;

const MAX_BPS: u32 = 10_000;
const MINUTES_PER_DAY: u16 = 1_440;

/// Per-symbol caps. The set of keys of `LimitsConfig::symbols` IS the allowlist
/// (control C03), so no symbol can be allowed without explicit caps.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SymbolLimits {
    pub max_order_qty_nanos: i64,
    pub max_order_notional_nanos: i64,
    pub max_position_nanos: i64,
}

/// Token bucket: `capacity` tokens, refilled at `refill_per_sec` tokens/second.
#[derive(Debug, Clone, Copy, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct RateLimit {
    pub capacity: u32,
    pub refill_per_sec: u32,
}

/// Trading session (control C05). Fixed UTC window: it does NOT follow DST or
/// the exchange holiday calendar. TODO(owner): calendar source.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SessionConfig {
    /// ISO weekdays, 1 = Monday .. 7 = Sunday.
    pub weekdays_utc: Vec<u8>,
    pub start_minute_utc: u16,
    pub end_minute_utc: u16,
}

/// Regime gate (control C18). See spec section 4.4.
#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct RegimeConfig {
    /// `C_MIN`: HMM posterior confidence below this holds the order.
    pub min_confidence: f64,
    /// Regimes the strategy is validated for (`R_s`), by RegimeLabel name.
    pub allowed: Vec<String>,
}

/// PROPOSED timing values (spec sections 4.1, 4.6, 5.2, 7). All have safe
/// defaults; all are TODO(owner) to confirm.
#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields, default)]
pub struct Timings {
    pub clock_skew_ms: u64,
    pub max_signal_age_ms: u64,
    pub max_ref_age_ms: u64,
    /// Not in the spec: freshness bound for Aegis's own regime label.
    pub max_regime_age_ms: u64,
    pub attestation_ttl_ms: u64,
    pub hold_window_ms: u64,
    pub replay_retention_ms: u64,
    pub operator_heartbeat_interval_ms: u64,
    pub operator_heartbeat_warn_ms: u64,
    pub liveness_trip_ms: u64,
    /// Max age of a signed reset approval.
    pub approval_max_age_ms: u64,
    /// C19: minimum trailing orders before the size distribution is trusted.
    pub unusual_size_min_history: u32,
    pub unusual_size_window_days: u32,
}

impl Default for Timings {
    fn default() -> Self {
        Timings {
            clock_skew_ms: 250,
            max_signal_age_ms: 5_000,
            max_ref_age_ms: 1_000,
            max_regime_age_ms: 60_000,
            attestation_ttl_ms: 5_000,
            hold_window_ms: 60_000,
            replay_retention_ms: 24 * 3_600_000,
            operator_heartbeat_interval_ms: 4 * 3_600_000,
            operator_heartbeat_warn_ms: 3 * 3_600_000 + 1_800_000,
            liveness_trip_ms: 60_000,
            approval_max_age_ms: 300_000,
            unusual_size_min_history: 30,
            unusual_size_window_days: 30,
        }
    }
}

#[derive(Debug, Clone, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LimitsConfig {
    /// C03 allowlist with per-symbol caps (C10, C11, C13).
    pub symbols: BTreeMap<String, SymbolLimits>,
    /// C04. Must be stated explicitly; enabling requires legal review.
    pub short_selling_enabled: bool,
    /// Accept deprecated `double` money fields converted at the boundary.
    pub accept_legacy_double_fields: bool,
    pub session: SessionConfig,
    /// C09 collar around Aegis-side mid, basis points.
    pub price_collar_bps: u32,
    /// C12 order size cap as basis points of `adv_30d`.
    pub max_order_adv_bps: u32,
    /// C14 gross exposure cap, nanos of account currency.
    pub max_gross_exposure_nanos: i64,
    /// C15 daily drawdown, basis points of starting daily NAV.
    pub max_daily_drawdown_bps: u32,
    /// C16 global and per-symbol token buckets.
    pub rate_global: RateLimit,
    pub rate_per_symbol: RateLimit,
    /// C17 abstain threshold on omega, in (0, 1].
    pub omega_min: f64,
    pub regime: RegimeConfig,
    /// Whether ResolveHold needs a distinct second approver. TODO(owner).
    pub hold_requires_second_approver: bool,
    /// Whether ResolveHold needs `cooling_period_enforced`. TODO(owner).
    pub hold_requires_cooling_period: bool,
    /// Reverse-guardrail distress score at/above which release is refused.
    pub hold_max_distress_score: f64,
    /// If true, an audit sink failure rejects the decision (fail closed).
    pub audit_mandatory: bool,
    #[serde(default)]
    pub timings: Timings,
}

/// A validated config plus the SHA-256 of the exact bytes it was parsed from.
#[derive(Debug, Clone)]
pub struct Limits {
    pub config: LimitsConfig,
    pub sha256_hex: String,
}

impl Limits {
    pub fn from_path(path: &Path) -> Result<Limits, ConfigError> {
        let bytes = std::fs::read(path).map_err(|e| ConfigError::Unreadable {
            what: "limits file",
            detail: e.to_string(),
        })?;
        Limits::from_bytes(&bytes)
    }

    pub fn from_bytes(bytes: &[u8]) -> Result<Limits, ConfigError> {
        let config: LimitsConfig =
            serde_json::from_slice(bytes).map_err(|e| ConfigError::Invalid(e.to_string()))?;
        config.validate()?;
        let digest = Sha256::digest(bytes);
        Ok(Limits {
            config,
            sha256_hex: hex::encode(&digest),
        })
    }
}

fn require(cond: bool, msg: &str) -> Result<(), ConfigError> {
    if cond {
        Ok(())
    } else {
        Err(ConfigError::Invalid(msg.to_owned()))
    }
}

impl LimitsConfig {
    /// Structural validation. Zero or negative limits are refused: a limit of
    /// zero would silently block everything, a negative one is meaningless.
    pub fn validate(&self) -> Result<(), ConfigError> {
        self.validate_symbols()?;
        self.validate_session()?;
        self.validate_scalars()?;
        self.validate_timings()
    }

    fn validate_symbols(&self) -> Result<(), ConfigError> {
        require(!self.symbols.is_empty(), "symbols must not be empty")?;
        for (sym, l) in &self.symbols {
            require(
                !sym.is_empty()
                    && sym
                        .chars()
                        .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '.'),
                "symbol keys must be non-empty upper-case tickers",
            )?;
            require(l.max_order_qty_nanos > 0, "max_order_qty_nanos must be > 0")?;
            require(
                l.max_order_notional_nanos > 0,
                "max_order_notional_nanos must be > 0",
            )?;
            require(l.max_position_nanos > 0, "max_position_nanos must be > 0")?;
        }
        Ok(())
    }

    fn validate_session(&self) -> Result<(), ConfigError> {
        let s = &self.session;
        require(
            !s.weekdays_utc.is_empty(),
            "session.weekdays_utc must not be empty",
        )?;
        require(
            s.weekdays_utc.iter().all(|d| (1..=7).contains(d)),
            "session.weekdays_utc must be 1..=7",
        )?;
        require(
            s.start_minute_utc < MINUTES_PER_DAY && s.end_minute_utc <= MINUTES_PER_DAY,
            "session minutes out of range",
        )?;
        require(
            s.start_minute_utc < s.end_minute_utc,
            "session start must be before end",
        )
    }

    fn validate_scalars(&self) -> Result<(), ConfigError> {
        require(
            self.price_collar_bps > 0 && self.price_collar_bps <= MAX_BPS,
            "price_collar_bps must be in 1..=10000",
        )?;
        require(
            self.max_order_adv_bps > 0 && self.max_order_adv_bps <= MAX_BPS,
            "max_order_adv_bps must be in 1..=10000",
        )?;
        require(
            self.max_gross_exposure_nanos > 0,
            "max_gross_exposure_nanos must be > 0",
        )?;
        require(
            self.max_daily_drawdown_bps > 0 && self.max_daily_drawdown_bps <= MAX_BPS,
            "max_daily_drawdown_bps must be in 1..=10000",
        )?;
        for r in [&self.rate_global, &self.rate_per_symbol] {
            require(
                r.capacity > 0 && r.refill_per_sec > 0,
                "rate limits must be > 0",
            )?;
        }
        require(
            self.omega_min.is_finite() && self.omega_min > 0.0 && self.omega_min <= 1.0,
            "omega_min must be in (0, 1]",
        )?;
        let c = self.regime.min_confidence;
        require(
            c.is_finite() && c > 0.0 && c <= 1.0,
            "regime.min_confidence must be in (0, 1]",
        )?;
        require(
            !self.regime.allowed.is_empty(),
            "regime.allowed must not be empty",
        )?;
        let d = self.hold_max_distress_score;
        require(
            d.is_finite() && (0.0..=1.0).contains(&d),
            "hold_max_distress_score must be in [0, 1]",
        )
    }

    fn validate_timings(&self) -> Result<(), ConfigError> {
        let t = &self.timings;
        let all = [
            t.clock_skew_ms,
            t.max_signal_age_ms,
            t.max_ref_age_ms,
            t.max_regime_age_ms,
            t.attestation_ttl_ms,
            t.hold_window_ms,
            t.replay_retention_ms,
            t.operator_heartbeat_interval_ms,
            t.operator_heartbeat_warn_ms,
            t.liveness_trip_ms,
            t.approval_max_age_ms,
        ];
        require(all.iter().all(|v| *v > 0), "timings must be > 0")?;
        require(
            t.operator_heartbeat_warn_ms < t.operator_heartbeat_interval_ms,
            "heartbeat warning must precede the trip",
        )?;
        require(
            t.replay_retention_ms >= t.max_signal_age_ms + t.clock_skew_ms,
            "replay retention must cover max_signal_age + clock_skew",
        )?;
        require(
            t.unusual_size_min_history > 0 && t.unusual_size_window_days > 0,
            "unusual-size settings must be > 0",
        )
    }
}

#[cfg(test)]
pub(crate) fn test_limits_json() -> String {
    r#"{
      "symbols": {"AAPL": {"max_order_qty_nanos": 100000000000, "max_order_notional_nanos": 50000000000000, "max_position_nanos": 500000000000},
                  "MSFT": {"max_order_qty_nanos": 50000000000, "max_order_notional_nanos": 50000000000000, "max_position_nanos": 200000000000}},
      "short_selling_enabled": false,
      "accept_legacy_double_fields": false,
      "session": {"weekdays_utc": [1,2,3,4,5], "start_minute_utc": 810, "end_minute_utc": 1200},
      "price_collar_bps": 150,
      "max_order_adv_bps": 50,
      "max_gross_exposure_nanos": 1000000000000000,
      "max_daily_drawdown_bps": 200,
      "rate_global": {"capacity": 20, "refill_per_sec": 10},
      "rate_per_symbol": {"capacity": 5, "refill_per_sec": 2},
      "omega_min": 0.55,
      "regime": {"min_confidence": 0.6, "allowed": ["TRENDING_BULL", "LOW_VOL_CHOP"]},
      "hold_requires_second_approver": false,
      "hold_requires_cooling_period": false,
      "hold_max_distress_score": 0.8,
      "audit_mandatory": true
    }"#
    .to_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(json: &str) -> Result<Limits, ConfigError> {
        Limits::from_bytes(json.as_bytes())
    }

    #[test]
    fn valid_config_parses_and_hashes_raw_bytes() {
        let json = test_limits_json();
        let l = parse(&json).unwrap();
        assert_eq!(l.config.symbols.len(), 2);
        assert_eq!(l.sha256_hex.len(), 64);
        assert_eq!(l.sha256_hex, parse(&json).unwrap().sha256_hex);
        assert_ne!(
            l.sha256_hex,
            parse(&json.replace("150", "151")).unwrap().sha256_hex
        );
    }

    #[test]
    fn every_required_field_missing_refuses_start() {
        let base: serde_json::Value = serde_json::from_str(&test_limits_json()).unwrap();
        let required: Vec<String> = base
            .as_object()
            .unwrap()
            .keys()
            .filter(|k| k.as_str() != "timings")
            .cloned()
            .collect();
        assert!(required.len() >= 14);
        for key in required {
            let mut v = base.clone();
            v.as_object_mut().unwrap().remove(&key);
            assert!(
                parse(&v.to_string()).is_err(),
                "missing {key} must refuse start"
            );
        }
    }

    #[test]
    fn empty_object_and_garbage_refuse_start() {
        assert!(parse("{}").is_err());
        assert!(parse("").is_err());
        assert!(parse("not json").is_err());
    }

    #[test]
    fn unknown_field_is_refused() {
        let json = test_limits_json().replace(
            "\"audit_mandatory\": true",
            "\"audit_mandatory\": true, \"typo_limit\": 1",
        );
        assert!(parse(&json).is_err());
    }

    #[test]
    fn zero_and_negative_limits_are_refused() {
        for (from, to) in [
            ("\"price_collar_bps\": 150", "\"price_collar_bps\": 0"),
            ("\"price_collar_bps\": 150", "\"price_collar_bps\": 10001"),
            (
                "\"max_daily_drawdown_bps\": 200",
                "\"max_daily_drawdown_bps\": 0",
            ),
            (
                "\"max_gross_exposure_nanos\": 1000000000000000",
                "\"max_gross_exposure_nanos\": -5",
            ),
            (
                "\"max_position_nanos\": 500000000000",
                "\"max_position_nanos\": 0",
            ),
            ("\"omega_min\": 0.55", "\"omega_min\": 0.0"),
            ("\"omega_min\": 0.55", "\"omega_min\": 1.5"),
            ("\"capacity\": 20", "\"capacity\": 0"),
            ("[1,2,3,4,5]", "[]"),
            ("[1,2,3,4,5]", "[0]"),
            ("\"start_minute_utc\": 810", "\"start_minute_utc\": 1300"),
        ] {
            let json = test_limits_json().replace(from, to);
            assert!(parse(&json).is_err(), "{to} must be refused");
        }
    }

    #[test]
    fn timing_defaults_are_the_proposed_values() {
        let t = parse(&test_limits_json()).unwrap().config.timings;
        assert_eq!(t.clock_skew_ms, 250);
        assert_eq!(t.max_signal_age_ms, 5_000);
        assert_eq!(t.operator_heartbeat_interval_ms, 14_400_000);
        assert_eq!(t.liveness_trip_ms, 60_000);
    }

    #[test]
    fn timing_override_must_stay_consistent() {
        let json = test_limits_json().replace(
            "\"audit_mandatory\": true",
            "\"audit_mandatory\": true, \"timings\": {\"operator_heartbeat_warn_ms\": 99999999999}",
        );
        assert!(parse(&json).is_err());
    }
}
