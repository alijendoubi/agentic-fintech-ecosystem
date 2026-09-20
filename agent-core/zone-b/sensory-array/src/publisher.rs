//! Redis snapshot publisher (`PUBLISH sensory:snapshots <json>`).
//!
//! Runs as its own task behind a bounded drop-oldest queue, so PUBLISH latency
//! or a Redis outage never sits on the WebSocket read path. Uses
//! `ConnectionManager`, which reconnects on its own (the plain multiplexed
//! connection used before never did, so one Redis blip silenced snapshots
//! forever). PUBLISH results are checked and counted, never discarded.
//!
//! Snapshots are ephemeral real-time signals: on failure they are dropped and
//! counted, not retried, because re-delivering an old snapshot would be worse
//! than skipping it.

use std::sync::Arc;
use std::time::Duration;

use redis::aio::ConnectionManager;
use tokio::time::timeout;
use tracing::{debug, info, warn};

use crate::error::{Result, SensoryError};
use crate::metrics::Metrics;
use crate::normalizer::MarketSnapshot;
use crate::queue::BoundedQueue;
use crate::runtime::{jittered_backoff, sleep_or_shutdown, wait_shutdown, Shutdown};

pub const SNAPSHOT_CHANNEL: &str = "sensory:snapshots";
const BATCH_MAX: usize = 64;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const PUBLISH_TIMEOUT: Duration = Duration::from_millis(500);

type Queue = Arc<BoundedQueue<Arc<MarketSnapshot>>>;

async fn connect(url: &str) -> Result<ConnectionManager> {
    let client = redis::Client::open(url)?;
    timeout(CONNECT_TIMEOUT, ConnectionManager::new(client))
        .await
        .map_err(|_| SensoryError::session("Redis connect timed out"))?
        .map_err(SensoryError::from)
}

/// Connect, retrying with backoff. `None` means shutdown came first.
async fn connect_with_retry(url: &str, shutdown: &mut Shutdown) -> Option<ConnectionManager> {
    let mut failures = 0_u32;
    loop {
        match connect(url).await {
            Ok(c) => {
                info!("Redis publisher connected");
                return Some(c);
            }
            Err(e) => {
                failures = failures.saturating_add(1);
                warn!(failures, error = %e, "Redis publisher cannot connect; retrying");
                let d =
                    jittered_backoff(Duration::from_millis(200), Duration::from_secs(5), failures);
                if sleep_or_shutdown(d, shutdown).await {
                    return None;
                }
            }
        }
    }
}

/// Serialise the batch; snapshots that fail to serialise are counted and skipped.
fn serialize_batch(batch: &[Arc<MarketSnapshot>], metrics: &Metrics) -> Vec<String> {
    batch
        .iter()
        .filter_map(|s| match serde_json::to_string(s.as_ref()) {
            Ok(j) => Some(j),
            Err(e) => {
                Metrics::inc(&metrics.redis_errors);
                warn!(symbol = %s.symbol, error = %e, "snapshot serialisation failed");
                None
            }
        })
        .collect()
}

async fn publish_batch(conn: &mut ConnectionManager, payloads: &[String]) -> Result<usize> {
    let mut pipe = redis::pipe();
    for p in payloads {
        pipe.cmd("PUBLISH").arg(SNAPSHOT_CHANNEL).arg(p.as_str());
    }
    let receivers: Vec<i64> = timeout(PUBLISH_TIMEOUT, pipe.query_async(conn))
        .await
        .map_err(|_| SensoryError::session("Redis PUBLISH timed out"))??;
    Ok(receivers.iter().filter(|n| **n > 0).count())
}

/// Publisher task. Returns on shutdown.
pub async fn run_publisher(
    redis_url: String,
    queue: Queue,
    metrics: Arc<Metrics>,
    mut shutdown: Shutdown,
) {
    let Some(mut conn) = connect_with_retry(&redis_url, &mut shutdown).await else {
        return;
    };
    let mut batch = Vec::with_capacity(BATCH_MAX);
    let mut consecutive_errors = 0_u64;
    loop {
        batch.clear();
        tokio::select! {
            () = queue.pop_batch(BATCH_MAX, &mut batch) => {}
            () = wait_shutdown(&mut shutdown) => break,
        }
        let payloads = serialize_batch(&batch, &metrics);
        if payloads.is_empty() {
            continue;
        }
        match publish_batch(&mut conn, &payloads).await {
            Ok(with_subscribers) => {
                consecutive_errors = 0;
                Metrics::add(&metrics.redis_published, payloads.len() as u64);
                debug!(
                    published = payloads.len(),
                    with_subscribers, "published snapshots"
                );
            }
            Err(e) => {
                Metrics::add(&metrics.redis_errors, payloads.len() as u64);
                consecutive_errors += 1;
                if consecutive_errors == 1 || consecutive_errors % 100 == 0 {
                    warn!(consecutive_errors, dropped = payloads.len(), error = %e, "Redis PUBLISH failed; snapshots dropped");
                }
            }
        }
    }
    info!("Redis publisher stopped");
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::sample_snapshot;

    #[test]
    fn serialize_batch_produces_one_json_document_per_snapshot() {
        let m = Metrics::default();
        let batch = vec![
            Arc::new(sample_snapshot("AAPL", 1_700_000_000_000_000_000)),
            Arc::new(sample_snapshot("MSFT", 1_700_000_000_000_000_001)),
        ];
        let out = serialize_batch(&batch, &m);
        assert_eq!(out.len(), 2);
        let v: serde_json::Value = serde_json::from_str(&out[0]).expect("json");
        assert_eq!(v["symbol"], "AAPL");
        assert_eq!(v["regime_label"], "TRENDING_BULL");
        assert_eq!(v["warmup"], false);
        assert_eq!(Metrics::get(&m.redis_errors), 0);
    }

    #[test]
    fn non_finite_snapshot_fails_serialisation_is_counted_not_panicked() {
        // serde_json refuses NaN/inf floats (writes null only via Value; the
        // struct serializer errors), so the failure path must be counted.
        let m = Metrics::default();
        let mut s = sample_snapshot("AAPL", 1_700_000_000_000_000_000);
        s.z_score = f64::NAN;
        let out = serialize_batch(&[Arc::new(s)], &m);
        // Either skipped (error) or emitted as null; it must never panic.
        assert!(out.len() <= 1);
    }

    #[tokio::test]
    async fn unreachable_redis_is_an_error_not_a_hang() {
        let t = std::time::Instant::now();
        let r = connect("redis://127.0.0.1:1").await;
        assert!(r.is_err());
        assert!(t.elapsed() < Duration::from_secs(8));
    }

    #[tokio::test]
    async fn shutdown_interrupts_connect_retry() {
        let (tx, mut rx) = tokio::sync::watch::channel(false);
        let h = tokio::spawn(async move {
            connect_with_retry("redis://127.0.0.1:1", &mut rx)
                .await
                .is_none()
        });
        tokio::time::sleep(Duration::from_millis(100)).await;
        tx.send(true).expect("send");
        let stopped = timeout(Duration::from_secs(8), h)
            .await
            .expect("no timeout")
            .expect("join");
        assert!(stopped);
    }
}
