//! Supervisor configuration (`aegis supervisor`). Same discipline as the Aegis
//! server: every problem refuses to start (non-zero exit), nothing is clamped
//! silently, and plaintext is only accepted outside production.
//!
//! | Variable | Required | Meaning |
//! |---|---|---|
//! | `AEGIS_ENV` | no | as for the server; unset means production |
//! | `AEGIS_SUPERVISOR_TARGET` | yes | `https://host:port` of Aegis (`http://` only with insecure dev) |
//! | `AEGIS_SUPERVISOR_TLS_CA` | yes (TLS) | PEM CA that signed the Aegis server certificate |
//! | `AEGIS_SUPERVISOR_TLS_CERT`, `AEGIS_SUPERVISOR_TLS_KEY` | yes (TLS) | PEM client identity; its CN needs the `state-reader` and `kill-trigger` roles |
//! | `AEGIS_SUPERVISOR_TLS_DOMAIN` | no | server name to verify (default: host of the target) |
//! | `AEGIS_SUPERVISOR_INSECURE_DEV` | no | literal `1`: plaintext, refused in production |
//! | `AEGIS_SUPERVISOR_TRIP_AFTER_MS` | no | default 60000; must be within [10000, 60000] |
//! | `AEGIS_SUPERVISOR_PROBE_INTERVAL_MS` | no | default 1000; within [100, trip/4] |
//! | `AEGIS_SUPERVISOR_PROBE_TIMEOUT_MS` | no | default 2000; within [100, trip/4] |

use std::path::PathBuf;
use std::time::Duration;

use crate::config::{parse_env, required, Environment};
use crate::error::ConfigError;

/// Spec 5.3: HARD after 60 s of failed liveness probes. The spec value is the
/// ceiling (a longer window would break the requirement); it may only be
/// tightened, and never below the floor (a hair trigger would latch a HARD that
/// needs a three-person reset over a network blip).
pub const DEFAULT_TRIP_AFTER_MS: u64 = 60_000;
pub const MIN_TRIP_AFTER_MS: u64 = 10_000;
pub const MAX_TRIP_AFTER_MS: u64 = 60_000;
const DEFAULT_PROBE_INTERVAL_MS: u64 = 1_000;
const DEFAULT_PROBE_TIMEOUT_MS: u64 = 2_000;
const MIN_PROBE_MS: u64 = 100;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SupervisorTlsPaths {
    pub ca: PathBuf,
    pub cert: PathBuf,
    pub key: PathBuf,
    /// Server name to verify; `None` means the host part of the target.
    pub domain: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SupervisorTls {
    Mutual(SupervisorTlsPaths),
    /// Plaintext, DEV ONLY (never in production).
    InsecureDev,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SupervisorConfig {
    pub env: Environment,
    pub target: String,
    pub tls: SupervisorTls,
    pub trip_after_ms: u64,
    pub probe_interval: Duration,
    pub probe_timeout: Duration,
}

fn millis_in(
    get: &dyn Fn(&str) -> Option<String>,
    key: &'static str,
    default_ms: u64,
    min_ms: u64,
    max_ms: u64,
) -> Result<u64, ConfigError> {
    let ms = match get(key) {
        None => default_ms,
        Some(v) => v
            .trim()
            .parse::<u64>()
            .map_err(|_| ConfigError::Invalid(format!("{key} must be an integer number of ms")))?,
    };
    if ms < min_ms || ms > max_ms {
        return Err(ConfigError::Invalid(format!(
            "{key}={ms} is outside the allowed range [{min_ms}, {max_ms}] ms"
        )));
    }
    Ok(ms)
}

fn parse_tls(
    get: &dyn Fn(&str) -> Option<String>,
    env: Environment,
) -> Result<SupervisorTls, ConfigError> {
    if get("AEGIS_SUPERVISOR_INSECURE_DEV").as_deref() == Some("1") {
        if env.is_production() {
            return Err(ConfigError::Invalid(
                "AEGIS_SUPERVISOR_INSECURE_DEV=1 is refused when AEGIS_ENV is production (or unset)"
                    .into(),
            ));
        }
        return Ok(SupervisorTls::InsecureDev);
    }
    Ok(SupervisorTls::Mutual(SupervisorTlsPaths {
        ca: required(get, "AEGIS_SUPERVISOR_TLS_CA")?.into(),
        cert: required(get, "AEGIS_SUPERVISOR_TLS_CERT")?.into(),
        key: required(get, "AEGIS_SUPERVISOR_TLS_KEY")?.into(),
        domain: get("AEGIS_SUPERVISOR_TLS_DOMAIN").filter(|d| !d.trim().is_empty()),
    }))
}

impl SupervisorConfig {
    pub fn from_lookup(
        get: &dyn Fn(&str) -> Option<String>,
    ) -> Result<SupervisorConfig, ConfigError> {
        let env = parse_env(get)?;
        let target = required(get, "AEGIS_SUPERVISOR_TARGET")?.trim().to_owned();
        let tls = parse_tls(get, env)?;
        let scheme = match tls {
            SupervisorTls::Mutual(_) => "https://",
            SupervisorTls::InsecureDev => "http://",
        };
        if !target.starts_with(scheme) || target.len() == scheme.len() {
            return Err(ConfigError::Invalid(format!(
                "AEGIS_SUPERVISOR_TARGET must start with {scheme} and name a host:port"
            )));
        }
        let trip_after_ms = millis_in(
            get,
            "AEGIS_SUPERVISOR_TRIP_AFTER_MS",
            DEFAULT_TRIP_AFTER_MS,
            MIN_TRIP_AFTER_MS,
            MAX_TRIP_AFTER_MS,
        )?;
        let cap = trip_after_ms / 4;
        let interval = millis_in(
            get,
            "AEGIS_SUPERVISOR_PROBE_INTERVAL_MS",
            DEFAULT_PROBE_INTERVAL_MS,
            MIN_PROBE_MS,
            cap,
        )?;
        let timeout = millis_in(
            get,
            "AEGIS_SUPERVISOR_PROBE_TIMEOUT_MS",
            DEFAULT_PROBE_TIMEOUT_MS,
            MIN_PROBE_MS,
            cap,
        )?;
        Ok(SupervisorConfig {
            env,
            target,
            tls,
            trip_after_ms,
            probe_interval: Duration::from_millis(interval),
            probe_timeout: Duration::from_millis(timeout),
        })
    }

    pub fn from_env() -> Result<SupervisorConfig, ConfigError> {
        SupervisorConfig::from_lookup(&|k| std::env::var(k).ok())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn base() -> HashMap<&'static str, &'static str> {
        HashMap::from([
            ("AEGIS_ENV", "development"),
            ("AEGIS_SUPERVISOR_TARGET", "https://aegis:50051"),
            ("AEGIS_SUPERVISOR_TLS_CA", "/tls/ca.pem"),
            ("AEGIS_SUPERVISOR_TLS_CERT", "/tls/sup.pem"),
            ("AEGIS_SUPERVISOR_TLS_KEY", "/tls/sup.key"),
        ])
    }

    fn parse(m: &HashMap<&'static str, &'static str>) -> Result<SupervisorConfig, ConfigError> {
        SupervisorConfig::from_lookup(&|k| m.get(k).map(|v| (*v).to_owned()))
    }

    #[test]
    fn defaults_are_the_spec_window_with_sane_probing() {
        let c = parse(&base()).unwrap();
        assert_eq!(c.trip_after_ms, 60_000);
        assert_eq!(c.probe_interval, Duration::from_secs(1));
        assert_eq!(c.probe_timeout, Duration::from_secs(2));
        assert!(matches!(c.tls, SupervisorTls::Mutual(_)));
    }

    #[test]
    fn each_required_setting_missing_refuses_to_start() {
        for key in [
            "AEGIS_SUPERVISOR_TARGET",
            "AEGIS_SUPERVISOR_TLS_CA",
            "AEGIS_SUPERVISOR_TLS_CERT",
            "AEGIS_SUPERVISOR_TLS_KEY",
        ] {
            let mut m = base();
            m.remove(key);
            assert!(parse(&m).is_err(), "{key}");
        }
    }

    #[test]
    fn trip_window_is_bounded_and_never_clamped_silently() {
        for bad in ["9999", "60001", "0", "abc", "-5", ""] {
            let mut m = base();
            m.insert("AEGIS_SUPERVISOR_TRIP_AFTER_MS", bad);
            assert!(parse(&m).is_err(), "{bad:?}");
        }
        for ok in ["10000", "30000", "60000"] {
            let mut m = base();
            m.insert("AEGIS_SUPERVISOR_TRIP_AFTER_MS", ok);
            assert_eq!(parse(&m).unwrap().trip_after_ms, ok.parse::<u64>().unwrap());
        }
    }

    #[test]
    fn probe_interval_and_timeout_must_leave_room_inside_the_window() {
        let mut m = base();
        m.insert("AEGIS_SUPERVISOR_TRIP_AFTER_MS", "10000");
        m.insert("AEGIS_SUPERVISOR_PROBE_TIMEOUT_MS", "2501");
        assert!(parse(&m).is_err(), "timeout above trip/4");
        m.insert("AEGIS_SUPERVISOR_PROBE_TIMEOUT_MS", "2500");
        assert!(parse(&m).is_ok());
        m.insert("AEGIS_SUPERVISOR_PROBE_INTERVAL_MS", "99");
        assert!(parse(&m).is_err(), "interval below the floor");
    }

    #[test]
    fn insecure_dev_only_outside_production_and_scheme_must_match() {
        let mut m = base();
        m.insert("AEGIS_SUPERVISOR_INSECURE_DEV", "1");
        assert!(parse(&m).is_err(), "https target with plaintext mode");
        m.insert("AEGIS_SUPERVISOR_TARGET", "http://aegis:50051");
        assert_eq!(parse(&m).unwrap().tls, SupervisorTls::InsecureDev);
        m.remove("AEGIS_ENV");
        assert!(parse(&m).is_err(), "unset env is production");
        let mut m = base();
        m.insert("AEGIS_SUPERVISOR_TARGET", "http://aegis:50051");
        assert!(parse(&m).is_err(), "plaintext target with TLS mode");
        m.insert("AEGIS_SUPERVISOR_TARGET", "https://");
        assert!(parse(&m).is_err());
    }
}
