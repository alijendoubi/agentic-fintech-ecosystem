//! Small shared runtime helpers: shutdown signalling and reconnect backoff.

use std::time::Duration;

use tokio::sync::watch;

/// Shutdown signal: `true` once shutdown was requested.
pub type Shutdown = watch::Receiver<bool>;

/// Resolves when shutdown was requested (or the sender was dropped).
pub async fn wait_shutdown(rx: &mut Shutdown) {
    loop {
        if *rx.borrow() {
            return;
        }
        if rx.changed().await.is_err() {
            return;
        }
    }
}

/// Sleep for `d`; returns `true` if shutdown interrupted the sleep.
pub async fn sleep_or_shutdown(d: Duration, rx: &mut Shutdown) -> bool {
    tokio::select! {
        _ = tokio::time::sleep(d) => false,
        _ = wait_shutdown(rx) => true,
    }
}

/// Exponential backoff: `base * 2^(attempt-1)` capped at `max`, then scaled by
/// a jitter factor in [0.8, 1.2] derived from `jitter_unit` in [0, 1)
/// (0.5 => exactly the nominal delay). `attempt` starts at 1.
pub fn backoff_delay(base: Duration, max: Duration, attempt: u32, jitter_unit: f64) -> Duration {
    let exp = attempt.saturating_sub(1).min(32);
    let nominal = base
        .checked_mul(1_u32.checked_shl(exp).unwrap_or(u32::MAX))
        .unwrap_or(max)
        .min(max);
    let unit = if jitter_unit.is_finite() {
        jitter_unit.clamp(0.0, 1.0)
    } else {
        0.5
    };
    let factor = 1.0 + 0.2 * (2.0 * unit - 1.0);
    nominal.mul_f64(factor)
}

/// `backoff_delay` with real randomness.
pub fn jittered_backoff(base: Duration, max: Duration, attempt: u32) -> Duration {
    backoff_delay(base, max, attempt, fastrand::f64())
}

#[cfg(test)]
mod tests {
    use super::*;

    const S: Duration = Duration::from_secs(1);

    #[test]
    fn backoff_follows_spec_schedule_and_caps_at_16s() {
        let d = |a| backoff_delay(S, Duration::from_secs(16), a, 0.5);
        assert_eq!(d(1), S);
        assert_eq!(d(2), Duration::from_secs(2));
        assert_eq!(d(3), Duration::from_secs(4));
        assert_eq!(d(4), Duration::from_secs(8));
        assert_eq!(d(5), Duration::from_secs(16));
        assert_eq!(d(6), Duration::from_secs(16));
        assert_eq!(d(1_000_000), Duration::from_secs(16));
        assert_eq!(d(u32::MAX), Duration::from_secs(16));
    }

    #[test]
    fn jitter_is_within_plus_minus_20_percent() {
        let lo = backoff_delay(S, Duration::from_secs(16), 3, 0.0);
        let hi = backoff_delay(S, Duration::from_secs(16), 3, 1.0);
        assert_eq!(lo, Duration::from_millis(3_200));
        assert_eq!(hi, Duration::from_millis(4_800));
        for _ in 0..1_000 {
            let d = jittered_backoff(S, Duration::from_secs(16), 3);
            assert!(d >= lo && d <= hi, "{d:?}");
        }
    }

    #[test]
    fn nan_jitter_falls_back_to_nominal() {
        assert_eq!(backoff_delay(S, Duration::from_secs(16), 2, f64::NAN), Duration::from_secs(2));
    }

    #[tokio::test]
    async fn sleep_is_interrupted_by_shutdown() {
        let (tx, mut rx) = watch::channel(false);
        let h = tokio::spawn(async move { sleep_or_shutdown(Duration::from_secs(30), &mut rx).await });
        tokio::time::sleep(Duration::from_millis(20)).await;
        tx.send(true).expect("send");
        let interrupted = tokio::time::timeout(Duration::from_secs(2), h)
            .await
            .expect("no timeout")
            .expect("join");
        assert!(interrupted);
    }

    #[tokio::test]
    async fn sleep_completes_without_shutdown() {
        let (_tx, mut rx) = watch::channel(false);
        assert!(!sleep_or_shutdown(Duration::from_millis(10), &mut rx).await);
    }
}
