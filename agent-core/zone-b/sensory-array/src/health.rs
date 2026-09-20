//! Liveness signalling for the container HEALTHCHECK (no `pgrep`/procps needed).
//!
//! The ingestor calls `Health::beat()` whenever a frame (data, ping or pong)
//! arrives on an authenticated session. A heartbeat task copies that into a
//! timestamp file only while the beat is recent, so the file goes stale when
//! the feed stalls. `sensory-array --healthcheck` succeeds only if the file is
//! fresh.

use std::path::Path;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tracing::warn;

use crate::runtime::{unix_ns, wait_shutdown, Shutdown};

const HEARTBEAT_EVERY: Duration = Duration::from_secs(5);
/// A health file older than this fails `--healthcheck`.
pub const HEALTHCHECK_MAX_AGE_S: i64 = 30;

#[derive(Debug)]
pub struct Health {
    last_beat_ns: AtomicI64,
}

impl Health {
    /// Starts "alive as of now" so the container is not unhealthy while it is
    /// still making its first connection.
    pub fn new() -> Self {
        Self {
            last_beat_ns: AtomicI64::new(unix_ns()),
        }
    }

    pub fn beat(&self) {
        self.last_beat_ns.store(unix_ns(), Ordering::Relaxed);
    }

    pub fn age(&self, now_ns: i64) -> Duration {
        let last = self.last_beat_ns.load(Ordering::Relaxed);
        Duration::from_nanos(u64::try_from(now_ns.saturating_sub(last)).unwrap_or(0))
    }
}

impl Default for Health {
    fn default() -> Self {
        Self::new()
    }
}

/// Heartbeat task: refresh `path` every 5 s while the last beat is younger
/// than `alive_window`.
pub async fn run_heartbeat(
    path: String,
    health: Arc<Health>,
    alive_window: Duration,
    mut shutdown: Shutdown,
) {
    loop {
        if health.age(unix_ns()) <= alive_window {
            let stamp = (unix_ns() / 1_000_000_000).to_string();
            if let Err(e) = tokio::fs::write(&path, stamp).await {
                warn!(path = %path, error = %e, "cannot write health file");
            }
        }
        tokio::select! {
            () = tokio::time::sleep(HEARTBEAT_EVERY) => {}
            () = wait_shutdown(&mut shutdown) => return,
        }
    }
}

/// `--healthcheck`: `Ok(())` iff the health file exists and is fresh.
pub fn check_file(path: &Path, now_s: i64, max_age_s: i64) -> Result<(), String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    let stamp: i64 = text
        .trim()
        .parse()
        .map_err(|_| format!("{} does not contain a unix timestamp", path.display()))?;
    let age = now_s.saturating_sub(stamp);
    if !(0..=max_age_s).contains(&age) {
        return Err(format!("health file age {age}s outside [0, {max_age_s}]"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("sensory-health-{}-{name}", std::process::id()))
    }

    #[test]
    fn check_file_accepts_fresh_and_rejects_stale_missing_and_garbage() {
        let p = tmp("a");
        std::fs::write(&p, "1000").expect("write");
        assert!(check_file(&p, 1010, 30).is_ok());
        assert!(check_file(&p, 1031, 30).is_err(), "stale");
        assert!(
            check_file(&p, 900, 30).is_err(),
            "timestamp from the future"
        );
        std::fs::write(&p, "garbage").expect("write");
        assert!(check_file(&p, 1010, 30).is_err());
        std::fs::remove_file(&p).expect("cleanup");
        assert!(check_file(&p, 1010, 30).is_err(), "missing");
    }

    #[test]
    fn beat_resets_age() {
        let h = Health::new();
        let now = unix_ns();
        assert!(h.age(now + 10_000_000_000) >= Duration::from_secs(9));
        h.beat();
        assert!(h.age(unix_ns()) < Duration::from_secs(1));
    }

    #[tokio::test]
    async fn heartbeat_writes_only_while_alive() {
        let p = tmp("hb");
        let _ = std::fs::remove_file(&p);
        let (tx, rx) = tokio::sync::watch::channel(false);
        let h = Arc::new(Health::new());
        let task = tokio::spawn(run_heartbeat(
            p.to_string_lossy().into_owned(),
            Arc::clone(&h),
            Duration::from_secs(60),
            rx,
        ));
        tokio::time::sleep(Duration::from_millis(200)).await;
        assert!(check_file(&p, unix_ns() / 1_000_000_000, HEALTHCHECK_MAX_AGE_S).is_ok());
        tx.send(true).expect("send");
        task.await.expect("join");
        std::fs::remove_file(&p).expect("cleanup");

        // A dead feed (zero alive window) never writes.
        let p2 = tmp("dead");
        let _ = std::fs::remove_file(&p2);
        let (tx2, rx2) = tokio::sync::watch::channel(false);
        let dead = Arc::new(Health::new());
        tokio::time::sleep(Duration::from_millis(5)).await;
        let task = tokio::spawn(run_heartbeat(
            p2.to_string_lossy().into_owned(),
            dead,
            Duration::ZERO,
            rx2,
        ));
        tokio::time::sleep(Duration::from_millis(100)).await;
        assert!(!p2.exists());
        tx2.send(true).expect("send");
        task.await.expect("join");
    }
}
