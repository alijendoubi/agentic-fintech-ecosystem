//! Regime labels from Zone A (`regime:labels` Redis channel).
//!
//! Fail-closed rules:
//! * every entry is timestamped; an entry older than `max_age` is served as
//!   `REGIME_UNKNOWN`, so a dead subscriber / dead detector can never leave a
//!   stale label looking current;
//! * only the known label set is accepted, confidence must be in [0, 1], the
//!   symbol must pass the ticker grammar and the configured symbol filter, and
//!   the cache is bounded, so an arbitrary publisher cannot grow memory;
//! * the subscriber reconnects with backoff instead of ending on the first
//!   Redis blip.
//!
//! Known limit: a TCP connection that dies silently (no FIN/RST) is not
//! detected by the subscriber itself. The age limit above turns that into
//! `REGIME_UNKNOWN` labels rather than stale ones.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use futures_util::StreamExt;
use serde::Deserialize;
use tracing::{info, warn};

use crate::error::{Result, SensoryError};
use crate::metrics::Metrics;
use crate::runtime::{jittered_backoff, sleep_or_shutdown, wait_shutdown, Shutdown};
use crate::validate::{SymbolFilter, MAX_SYMBOLS};

pub const REGIME_CHANNEL: &str = "regime:labels";
pub const UNKNOWN_LABEL: &str = "REGIME_UNKNOWN";
pub const KNOWN_LABELS: [&str; 6] = [
    UNKNOWN_LABEL,
    "TRENDING_BULL",
    "TRENDING_BEAR",
    "HIGH_VOL_CHOP",
    "LOW_VOL_CHOP",
    "CRISIS",
];
/// A published timestamp may lead our clock by at most this much.
const MAX_FUTURE_SKEW_NS: i64 = 5_000_000_000;
const MAX_PAYLOAD_BYTES: usize = 4_096;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RegimeEntry {
    pub label: &'static str,
    pub confidence: f64,
    /// When Zone A produced the label (payload `ts_ns`, else our receive time).
    pub published_ns: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, thiserror::Error)]
pub enum RegimeReject {
    #[error("payload too large")]
    TooLarge,
    #[error("payload is not valid regime JSON")]
    Malformed,
    #[error("symbol rejected")]
    Symbol,
    #[error("unknown regime label")]
    Label,
    #[error("confidence outside [0, 1]")]
    Confidence,
    #[error("invalid or future timestamp")]
    Timestamp,
    #[error("regime cache full")]
    CacheFull,
    #[error("out-of-order regime label (older than the cached one)")]
    OutOfOrder,
}

#[derive(Deserialize)]
struct RawRegime {
    symbol: String,
    label: String,
    confidence: f64,
    #[serde(default)]
    ts_ns: Option<i64>,
}

/// Thread-safe, bounded, timestamped regime cache.
pub struct RegimeCache {
    entries: RwLock<HashMap<String, RegimeEntry>>,
    max_age_ns: i64,
    filter: SymbolFilter,
}

impl RegimeCache {
    pub fn new(max_age: Duration, filter: SymbolFilter) -> Self {
        Self {
            entries: RwLock::new(HashMap::new()),
            max_age_ns: i64::try_from(max_age.as_nanos()).unwrap_or(i64::MAX),
            filter,
        }
    }

    /// Validate `payload` and store the label.
    pub fn ingest(&self, payload: &str, now_ns: i64) -> std::result::Result<(), RegimeReject> {
        let (symbol, entry) = self.parse(payload, now_ns)?;
        let mut map = self.entries.write().unwrap_or_else(|p| p.into_inner());
        if !map.contains_key(&symbol) && map.len() >= MAX_SYMBOLS {
            return Err(RegimeReject::CacheFull);
        }
        // Monotonic guard: a delayed or replayed older label must not
        // overwrite a newer one. Equal timestamps: last write wins.
        if let Some(cur) = map.get(&symbol) {
            if entry.published_ns < cur.published_ns {
                return Err(RegimeReject::OutOfOrder);
            }
        }
        map.insert(symbol, entry);
        Ok(())
    }

    fn parse(
        &self,
        payload: &str,
        now_ns: i64,
    ) -> std::result::Result<(String, RegimeEntry), RegimeReject> {
        if payload.len() > MAX_PAYLOAD_BYTES {
            return Err(RegimeReject::TooLarge);
        }
        let raw: RawRegime = serde_json::from_str(payload).map_err(|_| RegimeReject::Malformed)?;
        if !self.filter.allows(&raw.symbol) {
            return Err(RegimeReject::Symbol);
        }
        let label = KNOWN_LABELS
            .iter()
            .find(|l| **l == raw.label)
            .ok_or(RegimeReject::Label)?;
        if !raw.confidence.is_finite() || !(0.0..=1.0).contains(&raw.confidence) {
            return Err(RegimeReject::Confidence);
        }
        let published_ns = match raw.ts_ns {
            None => now_ns,
            Some(ts) if ts > 0 && ts <= now_ns.saturating_add(MAX_FUTURE_SKEW_NS) => ts,
            Some(_) => return Err(RegimeReject::Timestamp),
        };
        let entry = RegimeEntry {
            label,
            confidence: raw.confidence,
            published_ns,
        };
        Ok((raw.symbol, entry))
    }

    /// Current label for `symbol`, or `(REGIME_UNKNOWN, 0.0)` if absent/stale.
    pub fn lookup(&self, symbol: &str, now_ns: i64) -> (&'static str, f64) {
        let map = self.entries.read().unwrap_or_else(|p| p.into_inner());
        match map.get(symbol) {
            Some(e) if now_ns.saturating_sub(e.published_ns) <= self.max_age_ns => {
                (e.label, e.confidence)
            }
            _ => (UNKNOWN_LABEL, 0.0),
        }
    }

    pub fn len(&self) -> usize {
        self.entries.read().unwrap_or_else(|p| p.into_inner()).len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// Subscriber task: reconnects until shutdown.
pub async fn run_regime_subscriber(
    redis_url: String,
    cache: Arc<RegimeCache>,
    metrics: Arc<Metrics>,
    mut shutdown: Shutdown,
) {
    let mut failures = 0_u32;
    loop {
        let mut subscribed = false;
        let outcome =
            subscribe_once(&redis_url, &cache, &metrics, &mut shutdown, &mut subscribed).await;
        match outcome {
            Ok(true) => return,
            Ok(false) => warn!("regime subscription stream ended; reconnecting"),
            Err(e) => warn!(error = %e, "regime subscriber failed; reconnecting"),
        }
        failures = if subscribed {
            1
        } else {
            failures.saturating_add(1)
        };
        let delay = jittered_backoff(
            Duration::from_millis(500),
            Duration::from_secs(10),
            failures,
        );
        if sleep_or_shutdown(delay, &mut shutdown).await {
            return;
        }
    }
}

/// One subscription session. `Ok(true)` = shutdown requested.
async fn subscribe_once(
    redis_url: &str,
    cache: &RegimeCache,
    metrics: &Metrics,
    shutdown: &mut Shutdown,
    subscribed: &mut bool,
) -> Result<bool> {
    let client = redis::Client::open(redis_url)?;
    let mut pubsub = tokio::time::timeout(Duration::from_secs(5), client.get_async_pubsub())
        .await
        .map_err(|_| SensoryError::session("Redis pubsub connect timed out"))??;
    pubsub.subscribe(REGIME_CHANNEL).await?;
    *subscribed = true;
    info!(channel = REGIME_CHANNEL, "regime subscriber listening");

    let mut stream = pubsub.on_message();
    loop {
        tokio::select! {
            () = wait_shutdown(shutdown) => return Ok(true),
            msg = stream.next() => {
                let Some(msg) = msg else { return Ok(false) };
                handle_message(&msg, cache, metrics);
            }
        }
    }
}

fn handle_message(msg: &redis::Msg, cache: &RegimeCache, metrics: &Metrics) {
    let payload: String = match msg.get_payload() {
        Ok(p) => p,
        Err(e) => {
            Metrics::inc(&metrics.regime_rejected);
            warn!(error = %e, "regime payload is not a string; ignored");
            return;
        }
    };
    match cache.ingest(&payload, crate::runtime::unix_ns()) {
        Ok(()) => Metrics::inc(&metrics.regime_updates),
        Err(reject) => {
            Metrics::inc(&metrics.regime_rejected);
            if reject == RegimeReject::OutOfOrder {
                Metrics::inc(&metrics.regime_out_of_order);
            }
            warn!(reason = %reject, "regime label rejected");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const NOW: i64 = 1_700_000_000_000_000_000;

    fn cache() -> RegimeCache {
        RegimeCache::new(
            Duration::from_secs(15),
            SymbolFilter::from_symbols(&["AAPL".into(), "MSFT".into()]),
        )
    }

    fn payload(symbol: &str, label: &str, conf: &str, ts: Option<i64>) -> String {
        match ts {
            Some(t) => format!(
                r#"{{"symbol":"{symbol}","label":"{label}","confidence":{conf},"ts_ns":{t}}}"#
            ),
            None => format!(r#"{{"symbol":"{symbol}","label":"{label}","confidence":{conf}}}"#),
        }
    }

    #[test]
    fn accepts_the_format_zone_a_publishes() {
        let c = cache();
        c.ingest(
            &payload("AAPL", "TRENDING_BULL", "0.87", Some(NOW - 1_000)),
            NOW,
        )
        .expect("ok");
        assert_eq!(c.lookup("AAPL", NOW), ("TRENDING_BULL", 0.87));
    }

    #[test]
    fn unknown_symbol_returns_unknown() {
        assert_eq!(cache().lookup("AAPL", NOW), (UNKNOWN_LABEL, 0.0));
    }

    #[test]
    fn stale_entries_are_served_as_unknown() {
        let c = cache();
        c.ingest(&payload("AAPL", "CRISIS", "0.9", Some(NOW)), NOW)
            .expect("ok");
        assert_eq!(c.lookup("AAPL", NOW + 14_000_000_000).0, "CRISIS");
        assert_eq!(c.lookup("AAPL", NOW + 16_000_000_000), (UNKNOWN_LABEL, 0.0));
    }

    #[test]
    fn entry_without_timestamp_uses_receive_time() {
        let c = cache();
        c.ingest(&payload("AAPL", "LOW_VOL_CHOP", "0.5", None), NOW)
            .expect("ok");
        assert_eq!(c.lookup("AAPL", NOW + 1).0, "LOW_VOL_CHOP");
        assert_eq!(c.lookup("AAPL", NOW + 20_000_000_000).0, UNKNOWN_LABEL);
    }

    #[test]
    fn a_stale_publisher_timestamp_is_stale_even_if_just_received() {
        let c = cache();
        c.ingest(
            &payload("AAPL", "CRISIS", "0.9", Some(NOW - 60_000_000_000)),
            NOW,
        )
        .expect("ok");
        assert_eq!(c.lookup("AAPL", NOW).0, UNKNOWN_LABEL);
    }

    #[test]
    fn rejects_bad_payloads() {
        let c = cache();
        let cases: [(String, RegimeReject); 9] = [
            (payload("AAPL", "MOON", "0.5", None), RegimeReject::Label),
            (
                payload("AAPL", "CRISIS", "1.5", None),
                RegimeReject::Confidence,
            ),
            (
                payload("AAPL", "CRISIS", "-0.1", None),
                RegimeReject::Confidence,
            ),
            (payload("TSLA", "CRISIS", "0.5", None), RegimeReject::Symbol),
            (
                payload("AAPL\\nX", "CRISIS", "0.5", None),
                RegimeReject::Symbol,
            ),
            (
                payload("AAPL", "CRISIS", "0.5", Some(NOW + 60_000_000_000)),
                RegimeReject::Timestamp,
            ),
            (
                payload("AAPL", "CRISIS", "0.5", Some(-5)),
                RegimeReject::Timestamp,
            ),
            ("not json".to_string(), RegimeReject::Malformed),
            ("x".repeat(MAX_PAYLOAD_BYTES + 1), RegimeReject::TooLarge),
        ];
        for (p, want) in cases {
            assert_eq!(c.ingest(&p, NOW), Err(want), "{p}");
        }
        assert!(c.is_empty());
    }

    #[test]
    fn nan_confidence_is_rejected() {
        // JSON has no NaN literal, but a malicious/buggy publisher may send one
        // as a string or via a lenient encoder; both must be refused.
        let c = cache();
        assert!(c
            .ingest(
                r#"{"symbol":"AAPL","label":"CRISIS","confidence":"NaN"}"#,
                NOW
            )
            .is_err());
        assert!(c
            .ingest(
                r#"{"symbol":"AAPL","label":"CRISIS","confidence":NaN}"#,
                NOW
            )
            .is_err());
    }

    #[test]
    fn cache_is_bounded_with_wildcard_filter() {
        let c = RegimeCache::new(Duration::from_secs(15), SymbolFilter::Any);
        for i in 0..MAX_SYMBOLS {
            c.ingest(&payload(&format!("S{i}"), "CRISIS", "0.5", None), NOW)
                .expect("under bound");
        }
        assert_eq!(
            c.ingest(&payload("OVERFLOW", "CRISIS", "0.5", None), NOW),
            Err(RegimeReject::CacheFull)
        );
        // Updating an existing key is still allowed.
        assert!(c.ingest(&payload("S0", "CRISIS", "0.6", None), NOW).is_ok());
        assert_eq!(c.len(), MAX_SYMBOLS);
    }

    #[test]
    fn older_label_does_not_overwrite_newer() {
        let c = cache();
        c.ingest(&payload("AAPL", "TRENDING_BULL", "0.6", Some(NOW)), NOW)
            .expect("newer");
        // A delayed / replayed older label arrives afterwards.
        assert_eq!(
            c.ingest(
                &payload("AAPL", "CRISIS", "0.9", Some(NOW - 5_000_000_000)),
                NOW + 1,
            ),
            Err(RegimeReject::OutOfOrder)
        );
        assert_eq!(c.lookup("AAPL", NOW + 2), ("TRENDING_BULL", 0.6));
    }

    #[test]
    fn equal_timestamp_label_is_accepted_and_guard_is_per_symbol() {
        let c = cache();
        c.ingest(&payload("AAPL", "CRISIS", "0.9", Some(NOW)), NOW)
            .expect("first");
        c.ingest(&payload("AAPL", "LOW_VOL_CHOP", "0.4", Some(NOW)), NOW)
            .expect("equal ts");
        c.ingest(
            &payload("MSFT", "CRISIS", "0.9", Some(NOW - 1_000_000_000)),
            NOW,
        )
        .expect("other symbol unaffected");
        assert_eq!(c.lookup("AAPL", NOW), ("LOW_VOL_CHOP", 0.4));
    }

    #[test]
    fn newer_label_replaces_older() {
        let c = cache();
        c.ingest(&payload("AAPL", "CRISIS", "0.9", Some(NOW)), NOW)
            .expect("ok");
        c.ingest(
            &payload("AAPL", "TRENDING_BULL", "0.6", Some(NOW + 1)),
            NOW + 1,
        )
        .expect("ok");
        assert_eq!(c.lookup("AAPL", NOW + 2), ("TRENDING_BULL", 0.6));
    }
}
