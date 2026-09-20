//! Polygon WebSocket ingestor: connect, authenticate, subscribe, route quotes
//! and trades through the normaliser, TTL check and out to the sink queues.
//!
//! Reconnect policy (spec): exponential backoff 1s -> 16s cap with +-20%
//! jitter. Reconnecting never stops on its own: after `max_reconnect_attempts`
//! consecutive failed sessions a CRITICAL event is logged on every further
//! attempt. A session that lived at least `reconnect_stable` resets the
//! counter. Only bad credentials (fatal) or shutdown end `run`.
//!
//! Liveness: no frame for `ws_idle_timeout` triggers a WebSocket ping; no
//! frame (pong included) within another `ws_idle_timeout` drops the session.
//! Connecting and the handshake are bounded by timeouts too.
//!
//! The normaliser lives in the `Ingestor`, so rolling windows survive
//! reconnects. Sinks are non-blocking queues: nothing here awaits QuestDB or
//! Redis.

use std::borrow::Cow;
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{connect_async_with_config, MaybeTlsStream, WebSocketStream};
use tracing::{debug, error, info, warn};

use crate::config::Config;
use crate::error::{Result, SensoryError};
use crate::health::Health;
use crate::metrics::Metrics;
use crate::normalizer::{MarketSnapshot, Normalizer, QuoteInput, TradeInput};
use crate::polygon::{
    classify_handshake, parse_frame, parse_item, subscription_params, Handshake, PolyMsg,
};
use crate::questdb_writer::SnapshotQueue;
use crate::regime::RegimeCache;
use crate::runtime::{jittered_backoff, sleep_or_shutdown, unix_ns, wait_shutdown, Shutdown};
use crate::ttl::{TtlChecker, TtlLimits};
use crate::validate::{ms_to_ns, SymbolFilter};

type Ws = WebSocketStream<MaybeTlsStream<TcpStream>>;
type WsSink = SplitSink<Ws, Message>;
type WsSource = SplitStream<Ws>;

const NS_PER_DAY: i64 = 86_400 * 1_000_000_000;
const MAX_WS_MESSAGE_BYTES: usize = 8 << 20;
const CLOSE_TIMEOUT: Duration = Duration::from_secs(1);

/// Output queues (drop-oldest, never block the read loop).
#[derive(Clone)]
pub struct Sinks {
    pub questdb: SnapshotQueue,
    pub redis: SnapshotQueue,
}

/// How a session that did not fail ended.
#[derive(Debug, PartialEq, Eq)]
enum SessionEnd {
    Shutdown,
    /// Server closed the connection or the stream ended: reconnect.
    Closed,
}

struct SessionReport {
    /// Set once authenticated and subscribed.
    established_at: Option<Instant>,
    outcome: Result<SessionEnd>,
}

pub struct Ingestor {
    config: Config,
    regime: Arc<RegimeCache>,
    sinks: Sinks,
    metrics: Arc<Metrics>,
    health: Arc<Health>,
    normalizer: Normalizer,
    ttl: TtlChecker,
    filter: SymbolFilter,
}

impl Ingestor {
    pub fn new(
        config: Config,
        regime: Arc<RegimeCache>,
        sinks: Sinks,
        metrics: Arc<Metrics>,
        health: Arc<Health>,
    ) -> Self {
        let normalizer = Normalizer::new(config.rolling_window, config.adv_window_days);
        let ttl = TtlChecker::new(TtlLimits {
            l2_max_ms: config.freshness_l2_ms,
            trade_max_ms: config.freshness_print_ms,
            feed_max_lag_ms: config.feed_max_lag_ms,
            future_tolerance_ms: config.feed_future_tolerance_ms,
        });
        let filter = SymbolFilter::from_symbols(&config.symbols);
        Self {
            config,
            regime,
            sinks,
            metrics,
            health,
            normalizer,
            ttl,
            filter,
        }
    }

    /// Run until shutdown (`Ok`) or a fatal error such as bad credentials.
    pub async fn run(&mut self, shutdown: &mut Shutdown) -> Result<()> {
        let mut attempt = 0_u32;
        loop {
            if *shutdown.borrow() {
                return Ok(());
            }
            let report = self.run_session(shutdown).await;
            let stable = report
                .established_at
                .is_some_and(|t| t.elapsed() >= self.config.reconnect_stable());
            match report.outcome {
                Ok(SessionEnd::Shutdown) => return Ok(()),
                Ok(SessionEnd::Closed) => warn!("Polygon closed the connection; reconnecting"),
                Err(e) if e.is_fatal() => {
                    error!(error = %e, "fatal feed error; not retrying");
                    return Err(e);
                }
                Err(e) => warn!(error = %e, "feed session failed; reconnecting"),
            }
            Metrics::inc(&self.metrics.reconnects);
            attempt = if stable { 1 } else { attempt.saturating_add(1) };
            if attempt >= self.config.max_reconnect_attempts {
                error!(
                    consecutive_failures = attempt,
                    "CRITICAL: feed unavailable after repeated reconnect attempts; still retrying"
                );
            }
            let delay = jittered_backoff(
                self.config.reconnect_base(),
                self.config.reconnect_max(),
                attempt,
            );
            if sleep_or_shutdown(delay, shutdown).await {
                return Ok(());
            }
        }
    }

    async fn run_session(&mut self, shutdown: &mut Shutdown) -> SessionReport {
        let mut established_at = None;
        let outcome = self.session(shutdown, &mut established_at).await;
        SessionReport {
            established_at,
            outcome,
        }
    }

    async fn session(
        &mut self,
        shutdown: &mut Shutdown,
        established_at: &mut Option<Instant>,
    ) -> Result<SessionEnd> {
        let (mut sink, mut source) = self.connect_and_auth().await?;
        self.subscribe(&mut sink).await?;
        *established_at = Some(Instant::now());
        self.health.beat();
        self.pump(&mut sink, &mut source, shutdown).await
    }

    async fn connect_and_auth(&self) -> Result<(WsSink, WsSource)> {
        info!(url = %self.config.polygon_ws_url, "connecting to Polygon");
        let ws_config = WebSocketConfig {
            max_message_size: Some(MAX_WS_MESSAGE_BYTES),
            max_frame_size: Some(MAX_WS_MESSAGE_BYTES),
            ..WebSocketConfig::default()
        };
        let (ws, _) = timeout(
            self.config.ws_connect_timeout(),
            connect_async_with_config(&self.config.polygon_ws_url, Some(ws_config), false),
        )
        .await
        .map_err(|_| SensoryError::session("WebSocket connect timed out"))??;
        let (mut sink, mut source) = ws.split();

        // Sent immediately: Polygon queues it behind its own `connected` status.
        let auth = serde_json::json!({ "action": "auth", "params": self.config.polygon_api_key });
        sink.send(Message::Text(auth.to_string())).await?;
        timeout(self.config.ws_handshake_timeout(), await_auth(&mut source))
            .await
            .map_err(|_| SensoryError::session("timed out waiting for auth response"))??;
        info!("authenticated with Polygon");
        Ok((sink, source))
    }

    async fn subscribe(&self, sink: &mut WsSink) -> Result<()> {
        let params = subscription_params(&self.config.symbols);
        let msg = serde_json::json!({ "action": "subscribe", "params": params });
        sink.send(Message::Text(msg.to_string())).await?;
        info!(subscriptions = %params, "subscribed");
        Ok(())
    }

    /// Read loop with idle watchdog and shutdown handling.
    async fn pump(
        &mut self,
        sink: &mut WsSink,
        source: &mut WsSource,
        shutdown: &mut Shutdown,
    ) -> Result<SessionEnd> {
        let idle = self.config.ws_idle_timeout();
        let mut pinged = false;
        loop {
            tokio::select! {
                () = wait_shutdown(shutdown) => {
                    let _ = timeout(CLOSE_TIMEOUT, sink.send(Message::Close(None))).await;
                    return Ok(SessionEnd::Shutdown);
                }
                next = timeout(idle, source.next()) => match next {
                    Err(_) => {
                        if pinged {
                            return Err(SensoryError::session("no frames or pong within two idle periods"));
                        }
                        debug!("feed idle; pinging");
                        timeout(idle, sink.send(Message::Ping(Vec::new())))
                            .await
                            .map_err(|_| SensoryError::session("ping send timed out"))??;
                        pinged = true;
                    }
                    Ok(None) => return Ok(SessionEnd::Closed),
                    Ok(Some(Err(e))) => return Err(e.into()),
                    Ok(Some(Ok(msg))) => {
                        pinged = false;
                        self.health.beat();
                        if let Some(end) = self.on_message(msg, sink).await? {
                            return Ok(end);
                        }
                    }
                },
            }
        }
    }

    async fn on_message(&mut self, msg: Message, sink: &mut WsSink) -> Result<Option<SessionEnd>> {
        match msg {
            Message::Text(text) => self.handle_text(&text, unix_ns()),
            Message::Ping(payload) => sink.send(Message::Pong(payload)).await?,
            Message::Close(_) => return Ok(Some(SessionEnd::Closed)),
            Message::Binary(_) => {
                Metrics::inc(&self.metrics.bad_frames);
                debug!("unexpected binary frame ignored");
            }
            Message::Pong(_) | Message::Frame(_) => {}
        }
        Ok(None)
    }

    /// Process one text frame. A malformed frame is logged and skipped; it
    /// never tears the connection down.
    fn handle_text(&mut self, text: &str, recv_ns: i64) {
        Metrics::inc(&self.metrics.frames);
        let items = match parse_frame(text) {
            Ok(items) => items,
            Err(e) => {
                Metrics::inc(&self.metrics.bad_frames);
                let n = Metrics::get(&self.metrics.bad_frames);
                if n.is_power_of_two() {
                    warn!(count = n, error = %e, "non-array/invalid frame skipped");
                }
                return;
            }
        };
        for raw in items {
            match parse_item(raw) {
                Some(msg) => self.handle_item(msg, recv_ns),
                None => Metrics::inc(&self.metrics.rejected_messages),
            }
        }
    }

    fn handle_item(&mut self, msg: PolyMsg, recv_ns: i64) {
        if let PolyMsg::Status { status, message } = &msg {
            match classify_handshake(&msg) {
                Handshake::Ignore | Handshake::Authenticated => {
                    info!(%status, %message, "Polygon status")
                }
                Handshake::Rejected(_) | Handshake::Transient(_) => {
                    warn!(%status, %message, "Polygon status")
                }
            }
            return;
        }
        match msg {
            PolyMsg::Quote {
                ticker,
                bid_price,
                bid_size,
                ask_price,
                ask_size,
                timestamp_ms,
            } => {
                let quote = RawQuote {
                    bid_price,
                    bid_size,
                    ask_price,
                    ask_size,
                    timestamp_ms,
                };
                self.handle_quote(&ticker, quote, recv_ns);
            }
            PolyMsg::Trade {
                ticker,
                price,
                size,
                timestamp_ms,
            } => self.handle_trade(&ticker, price, size, timestamp_ms, recv_ns),
            PolyMsg::Status { .. } | PolyMsg::Unknown => {}
        }
    }

    fn reject(&self, reason: &str) {
        Metrics::inc(&self.metrics.rejected_messages);
        debug!(reason, "feed message rejected");
    }

    fn handle_quote(&mut self, ticker: &str, q: RawQuote, recv_ns: i64) {
        if !self.filter.allows(ticker) {
            return self.reject("symbol not allowed");
        }
        let Some(exchange_ts_ns) = ms_to_ns(q.timestamp_ms) else {
            return self.reject("invalid quote timestamp");
        };
        let input = QuoteInput {
            bid_price: q.bid_price,
            ask_price: q.ask_price,
            bid_size: q.bid_size,
            ask_size: q.ask_size,
            exchange_ts_ns,
            recv_ts_ns: recv_ns,
        };
        if let Err(reason) = self.normalizer.update_quote(ticker, input) {
            return self.reject(&reason.to_string());
        }
        Metrics::inc(&self.metrics.quotes);

        // `now` is taken per message: a slow earlier message in the same frame
        // must show up as staleness in later ones.
        let now_ns = unix_ns();
        let (label, confidence) = self.regime.lookup(ticker, now_ns);
        let Some(mut snap) =
            self.normalizer
                .snapshot(ticker, now_ns, Cow::Borrowed(label), confidence)
        else {
            return;
        };
        self.ttl.check(&mut snap, now_ns);
        self.emit(snap);
    }

    fn handle_trade(
        &mut self,
        ticker: &str,
        price: f64,
        size: f64,
        timestamp_ms: i64,
        recv_ns: i64,
    ) {
        if !self.filter.allows(ticker) {
            return self.reject("symbol not allowed");
        }
        let Some(exchange_ts_ns) = ms_to_ns(timestamp_ms) else {
            return self.reject("invalid trade timestamp");
        };
        let input = TradeInput {
            price,
            size,
            exchange_ts_ns,
            recv_ts_ns: recv_ns,
            day: exchange_ts_ns / NS_PER_DAY,
        };
        match self.normalizer.update_trade(ticker, input) {
            Ok(()) => Metrics::inc(&self.metrics.trades),
            Err(reason) => self.reject(&reason.to_string()),
        }
    }

    fn emit(&self, snap: MarketSnapshot) {
        if snap.is_stale {
            Metrics::inc(&self.metrics.stale_snapshots);
        }
        Metrics::inc(&self.metrics.snapshots);
        let snap = Arc::new(snap);
        self.sinks.questdb.push(Arc::clone(&snap));
        self.sinks.redis.push(snap);
    }
}

struct RawQuote {
    bid_price: f64,
    bid_size: f64,
    ask_price: f64,
    ask_size: f64,
    timestamp_ms: i64,
}

/// Read until Polygon confirms (or rejects) authentication.
async fn await_auth(source: &mut WsSource) -> Result<()> {
    loop {
        match source.next().await {
            None => return Err(SensoryError::session("connection closed during handshake")),
            Some(Err(e)) => return Err(e.into()),
            Some(Ok(Message::Close(_))) => {
                return Err(SensoryError::session(
                    "server closed connection during handshake",
                ))
            }
            Some(Ok(Message::Text(text))) => {
                if let Some(result) = auth_result(&text) {
                    return result;
                }
            }
            Some(Ok(_)) => {}
        }
    }
}

/// `Some(result)` once `text` carries a decisive handshake status.
fn auth_result(text: &str) -> Option<Result<()>> {
    let items = parse_frame(text).ok()?;
    for raw in items {
        let Some(msg) = parse_item(raw) else { continue };
        match classify_handshake(&msg) {
            Handshake::Authenticated => return Some(Ok(())),
            Handshake::Rejected(reason) => return Some(Err(SensoryError::AuthFailed { reason })),
            Handshake::Transient(reason) => return Some(Err(SensoryError::session(reason))),
            Handshake::Ignore => {}
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn auth_result_accepts_documented_and_rejects_failure() {
        let ok = r#"[{"ev":"status","status":"auth_success","message":"authenticated"}]"#;
        assert!(matches!(auth_result(ok), Some(Ok(()))));
        let bad = r#"[{"ev":"status","status":"auth_failed","message":"authentication failed"}]"#;
        assert!(matches!(
            auth_result(bad),
            Some(Err(SensoryError::AuthFailed { .. }))
        ));
        let limit = r#"[{"ev":"status","status":"max_connections","message":"Maximum number of websocket connections exceeded."}]"#;
        assert!(matches!(
            auth_result(limit),
            Some(Err(SensoryError::Session { .. }))
        ));
    }

    #[test]
    fn auth_result_ignores_connected_and_garbage() {
        let connected =
            r#"[{"ev":"status","status":"connected","message":"Connected Successfully"}]"#;
        assert!(auth_result(connected).is_none());
        assert!(auth_result("not json").is_none());
        assert!(auth_result(r#"{"ev":"status"}"#).is_none());
    }

    #[test]
    fn auth_result_sees_success_after_connected_in_same_frame() {
        let both =
            r#"[{"ev":"status","status":"connected"},{"ev":"status","status":"auth_success"}]"#;
        assert!(matches!(auth_result(both), Some(Ok(()))));
    }
}
