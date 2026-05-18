use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use tokio::sync::RwLock;
use tokio::time::sleep;
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{debug, error, info, warn};

use crate::config::Config;
use crate::error::{Result, SensoryError};
use crate::normalizer::{MarketSnapshot, Normalizer};
use crate::questdb_writer::QuestDbWriter;
use crate::ttl::TtlChecker;

// ─────────────────────────────────────────────────────────────
// Polygon.io WebSocket message types
// ─────────────────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
#[serde(tag = "ev")]
enum PolyMsg {
    /// Connection status
    #[serde(rename = "status")]
    Status { status: String, message: String },

    /// Auth result
    #[serde(rename = "auth")]
    Auth { status: String, message: String },

    /// NBBO Quote
    #[serde(rename = "Q")]
    Quote {
        #[serde(rename = "T")]
        ticker: String,
        #[serde(rename = "bp")]
        bid_price: f64,
        #[serde(rename = "bs")]
        bid_size: f64,
        #[serde(rename = "ap")]
        ask_price: f64,
        #[serde(rename = "as")]
        ask_size: f64,
        /// SIP timestamp (milliseconds)
        #[serde(rename = "t")]
        timestamp_ms: i64,
        /// Exchange timestamp (milliseconds)
        #[serde(rename = "y", default)]
        exchange_ts_ms: i64,
    },

    /// Trade print
    #[serde(rename = "T")]
    Trade {
        #[serde(rename = "T")]
        ticker: String,
        #[serde(rename = "p")]
        price: f64,
        #[serde(rename = "s")]
        size: f64,
        /// SIP timestamp (milliseconds)
        #[serde(rename = "t")]
        timestamp_ms: i64,
    },

    /// Catch-all for unknown event types
    #[serde(other)]
    Unknown,
}

// ─────────────────────────────────────────────────────────────
// Regime label cache (populated by regime subscriber side-task)
// ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone, Default)]
pub struct RegimeEntry {
    pub label: String,
    pub confidence: f64,
}

pub type RegimeCache = Arc<RwLock<HashMap<String, RegimeEntry>>>;

// ─────────────────────────────────────────────────────────────
// Main ingestor
// ─────────────────────────────────────────────────────────────

pub struct Ingestor {
    config: Config,
    regime_cache: RegimeCache,
}

impl Ingestor {
    pub fn new(config: Config, regime_cache: RegimeCache) -> Self {
        Self {
            config,
            regime_cache,
        }
    }

    /// Run the ingestor loop. Reconnects on failure with exponential backoff.
    pub async fn run(self) -> Result<()> {
        let mut attempt = 0u32;
        let mut delay_secs: f64 = 1.0;
        const MAX_DELAY: f64 = 16.0;

        loop {
            match self.run_once().await {
                Ok(()) => {
                    info!("Ingestor exited cleanly");
                    return Ok(());
                }
                Err(SensoryError::MaxReconnectExceeded { attempts }) => {
                    error!("Max reconnect attempts reached ({})", attempts);
                    return Err(SensoryError::MaxReconnectExceeded { attempts });
                }
                Err(e) => {
                    attempt += 1;
                    if attempt >= self.config.max_reconnect_attempts {
                        error!(
                            attempts = attempt,
                            "CRITICAL: Max reconnect attempts exceeded: {e}"
                        );
                        return Err(SensoryError::MaxReconnectExceeded { attempts: attempt });
                    }

                    // Jitter: ±20%
                    let jitter = delay_secs * 0.2 * (rand_jitter() - 0.5) * 2.0;
                    let actual_delay = (delay_secs + jitter).max(0.1);
                    warn!(
                        attempt,
                        delay_secs = actual_delay,
                        "WebSocket error, reconnecting: {e}"
                    );
                    sleep(Duration::from_secs_f64(actual_delay)).await;
                    delay_secs = (delay_secs * 2.0).min(MAX_DELAY);
                }
            }
        }
    }

    async fn run_once(&self) -> Result<()> {
        info!("Connecting to Polygon.io: {}", self.config.polygon_ws_url);

        let (ws_stream, _) = connect_async(&self.config.polygon_ws_url)
            .await
            .map_err(SensoryError::WebSocket)?;

        let (mut ws_sink, mut ws_source) = ws_stream.split();

        // Wait for "connected" status message
        self.wait_for_status(&mut ws_source, "connected").await?;

        // Authenticate
        let auth_msg = serde_json::json!({
            "action": "auth",
            "params": self.config.polygon_api_key,
        });
        ws_sink
            .send(Message::Text(auth_msg.to_string()))
            .await
            .map_err(SensoryError::WebSocket)?;

        self.wait_for_auth(&mut ws_source).await?;
        info!("Authenticated with Polygon.io");

        // Subscribe to quotes + trades for configured symbols
        let params = if self.config.symbols.contains(&"*".to_string()) {
            "Q.*,T.*".to_string()
        } else {
            self.config
                .symbols
                .iter()
                .flat_map(|s| [format!("Q.{s}"), format!("T.{s}")])
                .collect::<Vec<_>>()
                .join(",")
        };

        let sub_msg = serde_json::json!({ "action": "subscribe", "params": params });
        ws_sink
            .send(Message::Text(sub_msg.to_string()))
            .await
            .map_err(SensoryError::WebSocket)?;

        info!("Subscribed: {}", params);

        // Initialize processing components
        let mut normalizer = Normalizer::new(
            self.config.rolling_window,
            self.config.adv_window_days,
        );
        let ttl = TtlChecker::new(self.config.freshness_l2_ms, self.config.freshness_print_ms);
        let mut qdb = QuestDbWriter::new(
            self.config.questdb_ilp_host.clone(),
            self.config.questdb_ilp_port,
        );

        // Redis publisher for snapshots
        let redis_client = redis::Client::open(self.config.redis_url.clone())
            .map_err(SensoryError::Redis)?;
        let mut redis_conn = redis_client
            .get_multiplexed_async_connection()
            .await
            .map_err(SensoryError::Redis)?;

        // Message loop
        while let Some(msg) = ws_source.next().await {
            let msg = msg.map_err(SensoryError::WebSocket)?;

            let text = match msg {
                Message::Text(t) => t,
                Message::Ping(payload) => {
                    ws_sink
                        .send(Message::Pong(payload))
                        .await
                        .map_err(SensoryError::WebSocket)?;
                    continue;
                }
                Message::Close(_) => {
                    info!("Server closed WebSocket");
                    break;
                }
                _ => continue,
            };

            let now_ns = unix_ns();

            // Polygon sends arrays of messages
            let msgs: Vec<serde_json::Value> =
                serde_json::from_str(&text).map_err(SensoryError::Json)?;

            for raw in msgs {
                let poly_msg: PolyMsg = match serde_json::from_value(raw) {
                    Ok(m) => m,
                    Err(e) => {
                        debug!("Unrecognized message: {e}");
                        continue;
                    }
                };

                match poly_msg {
                    PolyMsg::Quote {
                        ticker,
                        bid_price,
                        bid_size,
                        ask_price,
                        ask_size,
                        timestamp_ms,
                        exchange_ts_ms,
                    } => {
                        let exchange_ts_ns = if exchange_ts_ms > 0 {
                            exchange_ts_ms * 1_000_000
                        } else {
                            timestamp_ms * 1_000_000
                        };

                        normalizer.update_quote(
                            &ticker,
                            bid_price,
                            ask_price,
                            bid_size,
                            ask_size,
                            exchange_ts_ns,
                        );

                        let (regime_label, regime_confidence) =
                            self.get_regime(&ticker).await;

                        if let Some(mut snap) = normalizer.snapshot(
                            &ticker,
                            now_ns,
                            regime_label,
                            regime_confidence,
                        ) {
                            ttl.check(&mut snap, now_ns);
                            self.emit(&mut qdb, &mut redis_conn, &snap).await;
                        }
                    }

                    PolyMsg::Trade {
                        ticker,
                        price,
                        size,
                        timestamp_ms,
                    } => {
                        let ts_ns = timestamp_ms * 1_000_000;
                        let day = day_of_year_from_ns(ts_ns);
                        normalizer.update_trade(&ticker, price, size, ts_ns, day);
                        debug!(symbol = %ticker, price, size, "Trade");
                    }

                    PolyMsg::Status { status, message } => {
                        info!("Polygon status: {} — {}", status, message);
                    }

                    PolyMsg::Auth { status, message } => {
                        info!("Polygon auth: {} — {}", status, message);
                    }

                    PolyMsg::Unknown => {}
                }
            }
        }

        qdb.flush().await?;
        qdb.close().await;
        Ok(())
    }

    async fn wait_for_status<S>(
        &self,
        source: &mut S,
        expected: &str,
    ) -> Result<()>
    where
        S: StreamExt<Item = std::result::Result<Message, tokio_tungstenite::tungstenite::Error>>
            + Unpin,
    {
        tokio::time::timeout(Duration::from_secs(10), async {
            while let Some(msg) = source.next().await {
                let msg = msg.map_err(SensoryError::WebSocket)?;
                if let Message::Text(t) = msg {
                    let v: Vec<serde_json::Value> = serde_json::from_str(&t)?;
                    for item in &v {
                        if item.get("ev").and_then(|e| e.as_str()) == Some("status") {
                            let status = item
                                .get("status")
                                .and_then(|s| s.as_str())
                                .unwrap_or("");
                            if status == expected {
                                return Ok(());
                            }
                        }
                    }
                }
            }
            Err(SensoryError::AuthFailed {
                reason: "Connection closed before status received".into(),
            })
        })
        .await
        .map_err(|_| SensoryError::AuthFailed {
            reason: format!("Timeout waiting for status '{expected}'"),
        })?
    }

    async fn wait_for_auth<S>(&self, source: &mut S) -> Result<()>
    where
        S: StreamExt<Item = std::result::Result<Message, tokio_tungstenite::tungstenite::Error>>
            + Unpin,
    {
        tokio::time::timeout(Duration::from_secs(10), async {
            while let Some(msg) = source.next().await {
                let msg = msg.map_err(SensoryError::WebSocket)?;
                if let Message::Text(t) = msg {
                    let v: Vec<serde_json::Value> = serde_json::from_str(&t)?;
                    for item in &v {
                        let ev = item.get("ev").and_then(|e| e.as_str()).unwrap_or("");
                        let status = item
                            .get("status")
                            .and_then(|s| s.as_str())
                            .unwrap_or("");
                        if ev == "auth" {
                            if status == "auth_success" {
                                return Ok(());
                            } else {
                                return Err(SensoryError::AuthFailed {
                                    reason: format!("Auth failed: status={status}"),
                                });
                            }
                        }
                    }
                }
            }
            Err(SensoryError::AuthFailed {
                reason: "Connection closed before auth response".into(),
            })
        })
        .await
        .map_err(|_| SensoryError::AuthFailed {
            reason: "Timeout waiting for auth response".into(),
        })?
    }

    async fn get_regime(&self, symbol: &str) -> (String, f64) {
        let cache = self.regime_cache.read().await;
        cache
            .get(symbol)
            .map(|e| (e.label.clone(), e.confidence))
            .unwrap_or_else(|| ("REGIME_UNKNOWN".to_string(), 0.0))
    }

    async fn emit(
        &self,
        qdb: &mut QuestDbWriter,
        redis_conn: &mut redis::aio::MultiplexedConnection,
        snap: &MarketSnapshot,
    ) {
        // Write to QuestDB
        if let Err(e) = qdb.write(snap).await {
            error!(symbol = %snap.symbol, "QuestDB write failed: {e}");
        }

        // Publish to Redis channel for Zone A consumption
        let payload = match serde_json::to_string(snap) {
            Ok(p) => p,
            Err(e) => {
                error!("Failed to serialize snapshot: {e}");
                return;
            }
        };
        let _: std::result::Result<(), _> = redis::cmd("PUBLISH")
            .arg("sensory:snapshots")
            .arg(&payload)
            .query_async(redis_conn)
            .await;
    }
}

// ─────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────

fn unix_ns() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as i64
}

fn day_of_year_from_ns(ts_ns: i64) -> u32 {
    use chrono::{Datelike, TimeZone, Utc};
    let secs = ts_ns / 1_000_000_000;
    let dt = Utc.timestamp_opt(secs, 0).single().unwrap_or_default();
    dt.ordinal()
}

/// Lightweight pseudo-random jitter in [0, 1) without pulling in rand crate.
fn rand_jitter() -> f64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    let ns = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .subsec_nanos();
    (ns % 1000) as f64 / 1000.0
}
