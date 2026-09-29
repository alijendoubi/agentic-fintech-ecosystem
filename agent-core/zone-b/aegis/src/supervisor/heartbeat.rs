//! Supervising the Supervisor (ALI-163): a liveness heartbeat file.
//!
//! A restart policy only covers a Supervisor that EXITS. A Supervisor that is
//! alive but stuck (its loop no longer completing probe steps) would leave the
//! Aegis liveness backstop silently off. So after every completed supervision
//! step the Supervisor writes the current wall-clock time (unix ms) to
//! `AEGIS_SUPERVISOR_HEARTBEAT_FILE`, and `aegis supervisor-healthcheck` fails
//! when that file is missing, unparseable or older than
//! `AEGIS_SUPERVISOR_HEARTBEAT_MAX_AGE_MS`. The orchestrator's health status
//! (and its alerting on unhealthy / restarting containers) is then the external
//! monitor. A step counts whatever Aegis's state: a Supervisor that is watching
//! a dead Aegis, or has tripped HARD, is doing its job and stays healthy.
//!
//! The write is atomic (temp file + rename) so the healthcheck never reads a
//! torn value. A failed write is logged and retried on the next step; it makes
//! the healthcheck fail, which is the point.

use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::error::ConfigError;

/// Default staleness limit: comfortably above one probe interval plus one probe
/// timeout at their defaults (1 s + 2 s), well below the 60 s trip window.
pub const DEFAULT_MAX_AGE_MS: u64 = 10_000;
pub const MIN_MAX_AGE_MS: u64 = 1_000;
pub const MAX_MAX_AGE_MS: u64 = 60_000;

pub const FILE_ENV: &str = "AEGIS_SUPERVISOR_HEARTBEAT_FILE";
pub const MAX_AGE_ENV: &str = "AEGIS_SUPERVISOR_HEARTBEAT_MAX_AGE_MS";

/// Why a heartbeat is not fresh. Carried for the healthcheck's stderr only.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Staleness {
    Unparseable,
    TooOld { age_ms: u64 },
    InTheFuture { ahead_ms: u64 },
}

/// Pure freshness rule. A timestamp more than `max_age_ms` in the future is
/// also refused: a wall-clock jump must not make a dead Supervisor look alive.
pub fn check(content: &str, now_ms: u64, max_age_ms: u64) -> Result<(), Staleness> {
    let beat: u64 = content.trim().parse().map_err(|_| Staleness::Unparseable)?;
    if beat > now_ms {
        let ahead_ms = beat - now_ms;
        return if ahead_ms > max_age_ms {
            Err(Staleness::InTheFuture { ahead_ms })
        } else {
            Ok(())
        };
    }
    let age_ms = now_ms - beat;
    if age_ms > max_age_ms {
        return Err(Staleness::TooOld { age_ms });
    }
    Ok(())
}

pub fn unix_now_ms() -> Option<u64> {
    let d = SystemTime::now().duration_since(UNIX_EPOCH).ok()?;
    u64::try_from(d.as_millis()).ok()
}

/// Writes the heartbeat file.
#[derive(Debug, Clone)]
pub struct Heartbeat {
    path: PathBuf,
}

impl Heartbeat {
    pub fn new(path: PathBuf) -> Heartbeat {
        Heartbeat { path }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Record one completed step at `now_ms`. Never panics; errors are returned
    /// for the caller to log.
    pub fn beat(&self, now_ms: u64) -> std::io::Result<()> {
        let mut tmp = self.path.clone().into_os_string();
        tmp.push(".tmp");
        let tmp = PathBuf::from(tmp);
        {
            let mut f = std::fs::File::create(&tmp)?;
            writeln!(f, "{now_ms}")?;
            f.sync_all()?;
        }
        std::fs::rename(&tmp, &self.path)
    }
}

/// `AEGIS_SUPERVISOR_HEARTBEAT_MAX_AGE_MS`, bounded and never clamped silently.
pub fn max_age_from_lookup(get: &dyn Fn(&str) -> Option<String>) -> Result<u64, ConfigError> {
    let ms = match get(MAX_AGE_ENV) {
        None => DEFAULT_MAX_AGE_MS,
        Some(v) => v.trim().parse::<u64>().map_err(|_| {
            ConfigError::Invalid(format!("{MAX_AGE_ENV} must be an integer number of ms"))
        })?,
    };
    if !(MIN_MAX_AGE_MS..=MAX_MAX_AGE_MS).contains(&ms) {
        return Err(ConfigError::Invalid(format!(
            "{MAX_AGE_ENV}={ms} is outside the allowed range [{MIN_MAX_AGE_MS}, {MAX_MAX_AGE_MS}] ms"
        )));
    }
    Ok(ms)
}

/// `aegis supervisor-healthcheck`: 0 fresh, 1 stale / missing, 2 misconfigured.
pub fn healthcheck_exit_code(get: &dyn Fn(&str) -> Option<String>) -> u8 {
    let Some(path) = get(FILE_ENV).filter(|p| !p.trim().is_empty()) else {
        eprintln!("{FILE_ENV} is not set");
        return 2;
    };
    let max_age_ms = match max_age_from_lookup(get) {
        Ok(ms) => ms,
        Err(e) => {
            eprintln!("{e}");
            return 2;
        }
    };
    let Some(now_ms) = unix_now_ms() else {
        eprintln!("cannot read the wall clock");
        return 1;
    };
    let content = match std::fs::read_to_string(path.trim()) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("no heartbeat: {e}");
            return 1;
        }
    };
    match check(&content, now_ms, max_age_ms) {
        Ok(()) => 0,
        Err(why) => {
            eprintln!("supervisor heartbeat not fresh: {why:?}");
            1
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    #[test]
    fn fresh_within_the_limit() {
        assert_eq!(check("1000\n", 5_000, 10_000), Ok(()));
        assert_eq!(check("5000", 5_000, 10_000), Ok(()));
    }

    #[test]
    fn stale_beyond_the_limit() {
        assert_eq!(
            check("1000", 11_001, 10_000),
            Err(Staleness::TooOld { age_ms: 10_001 })
        );
    }

    #[test]
    fn garbage_is_not_fresh() {
        for bad in ["", "abc", "-5", "1.5", "  "] {
            assert_eq!(
                check(bad, 5_000, 10_000),
                Err(Staleness::Unparseable),
                "{bad:?}"
            );
        }
    }

    #[test]
    fn far_future_timestamp_is_refused() {
        assert_eq!(check("5500", 5_000, 10_000), Ok(()), "small skew tolerated");
        assert_eq!(
            check("20001", 5_000, 10_000),
            Err(Staleness::InTheFuture { ahead_ms: 15_001 })
        );
    }

    #[test]
    fn beat_then_healthcheck_round_trip() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("hb");
        let hb = Heartbeat::new(path.clone());
        let env = HashMap::from([(FILE_ENV, path.to_string_lossy().into_owned())]);
        let get = |k: &str| env.get(k).cloned();

        assert_eq!(healthcheck_exit_code(&get), 1, "missing file is unhealthy");
        hb.beat(unix_now_ms().unwrap()).unwrap();
        assert_eq!(healthcheck_exit_code(&get), 0);
        hb.beat(unix_now_ms().unwrap() - 60_000).unwrap();
        assert_eq!(healthcheck_exit_code(&get), 1, "old beat is unhealthy");
        assert!(
            !dir.path().join("hb.tmp").exists(),
            "temp file renamed away"
        );
    }

    #[test]
    fn healthcheck_configuration_errors_exit_2() {
        let none = |_: &str| None;
        assert_eq!(healthcheck_exit_code(&none), 2);
        let env = HashMap::from([
            (FILE_ENV, "/tmp/hb".to_owned()),
            (MAX_AGE_ENV, "500".to_owned()),
        ]);
        let get = |k: &str| env.get(k).cloned();
        assert_eq!(healthcheck_exit_code(&get), 2);
    }

    #[test]
    fn max_age_is_bounded_and_never_clamped() {
        for bad in ["999", "60001", "x", ""] {
            let env = HashMap::from([(MAX_AGE_ENV, bad.to_owned())]);
            assert!(
                max_age_from_lookup(&|k| env.get(k).cloned()).is_err(),
                "{bad:?}"
            );
        }
        assert_eq!(max_age_from_lookup(&|_| None).unwrap(), DEFAULT_MAX_AGE_MS);
    }
}
