//! Ingestor integration tests against a scripted local WebSocket server that
//! speaks Polygon's handshake (`connected` -> `auth_success` -> subscribe).

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::WebSocketStream;

use sensory_array::config::Config;
use sensory_array::error::SensoryError;
use sensory_array::health::Health;
use sensory_array::ingestor::{Ingestor, Sinks};
use sensory_array::metrics::Metrics;
use sensory_array::normalizer::MarketSnapshot;
use sensory_array::questdb_writer::SnapshotQueue;
use sensory_array::queue::BoundedQueue;
use sensory_array::regime::RegimeCache;
use sensory_array::runtime::unix_ns;
use sensory_array::validate::SymbolFilter;

const CONNECTED: &str =
    r#"[{"ev":"status","status":"connected","message":"Connected Successfully"}]"#;
const AUTH_OK: &str = r#"[{"ev":"status","status":"auth_success","message":"authenticated"}]"#;
const AUTH_FAIL: &str =
    r#"[{"ev":"status","status":"auth_failed","message":"authentication failed"}]"#;

#[derive(Clone, Copy)]
enum After {
    /// Drop the TCP connection without a WebSocket close handshake.
    Drop,
    /// Send a proper Close frame.
    Close,
    /// Keep the session open (reading, so pings are answered).
    Hold,
    /// Keep the socket open but never read or write (dead peer).
    Silent,
}

#[derive(Clone)]
struct Behavior {
    auth_ok: bool,
    frames: Vec<String>,
    after: After,
}

struct Server {
    port: u16,
    accepts: Arc<Mutex<Vec<Instant>>>,
}

impl Server {
    fn connections(&self) -> usize {
        self.accepts.lock().expect("lock").len()
    }
    fn accept_times(&self) -> Vec<Instant> {
        self.accepts.lock().expect("lock").clone()
    }
}

async fn wait_for_text(ws: &mut WebSocketStream<TcpStream>, needle: &str) -> bool {
    while let Ok(Some(Ok(msg))) = tokio::time::timeout(Duration::from_secs(2), ws.next()).await {
        if let Message::Text(t) = msg {
            if t.contains(needle) {
                return true;
            }
        }
    }
    false
}

async fn hold_reading(ws: &mut WebSocketStream<TcpStream>, total: Duration) {
    let end = Instant::now() + total;
    while let Some(left) = end.checked_duration_since(Instant::now()) {
        match tokio::time::timeout(left, ws.next()).await {
            Ok(Some(Ok(_))) => {}
            _ => return,
        }
    }
}

async fn serve(tcp: TcpStream, b: Behavior) {
    let Ok(mut ws) = tokio_tungstenite::accept_async(tcp).await else {
        return;
    };
    let _ = ws.send(Message::Text(CONNECTED.into())).await;
    if !wait_for_text(&mut ws, "\"action\":\"auth\"").await {
        return;
    }
    let reply = if b.auth_ok { AUTH_OK } else { AUTH_FAIL };
    let _ = ws.send(Message::Text(reply.into())).await;
    if !b.auth_ok {
        hold_reading(&mut ws, Duration::from_secs(3)).await;
        return;
    }
    if !wait_for_text(&mut ws, "Q.AAPL").await {
        return;
    }
    for f in &b.frames {
        let _ = ws.send(Message::Text(f.clone())).await;
    }
    match b.after {
        After::Drop => drop(ws),
        After::Close => {
            let _ = ws.close(None).await;
            hold_reading(&mut ws, Duration::from_millis(200)).await;
        }
        After::Hold => hold_reading(&mut ws, Duration::from_secs(30)).await,
        After::Silent => tokio::time::sleep(Duration::from_secs(30)).await,
    }
}

async fn start_server(script: impl Fn(usize) -> Behavior + Send + Sync + 'static) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let port = listener.local_addr().expect("addr").port();
    let accepts = Arc::new(Mutex::new(Vec::new()));
    let acc = Arc::clone(&accepts);
    tokio::spawn(async move {
        while let Ok((tcp, _)) = listener.accept().await {
            let idx = {
                let mut a = acc.lock().expect("lock");
                a.push(Instant::now());
                a.len() - 1
            };
            tokio::spawn(serve(tcp, script(idx)));
        }
    });
    Server { port, accepts }
}

fn quote_frame(bid: f64, ask: f64, t_ms: i64) -> String {
    format!(
        r#"[{{"ev":"Q","sym":"AAPL","bx":4,"bp":{bid},"bs":100,"ax":7,"ap":{ask},"as":100,"c":0,"i":[],"t":{t_ms},"z":3,"q":1}}]"#
    )
}

fn now_ms() -> i64 {
    unix_ns() / 1_000_000
}

fn behavior(frames: Vec<String>, after: After) -> Behavior {
    Behavior {
        auth_ok: true,
        frames,
        after,
    }
}

struct Harness {
    questdb: SnapshotQueue,
    metrics: Arc<Metrics>,
    shutdown: tokio::sync::watch::Sender<bool>,
    join: JoinHandle<Result<(), SensoryError>>,
}

impl Harness {
    fn snapshots(&self) -> Vec<Arc<MarketSnapshot>> {
        let mut v = Vec::new();
        self.questdb.drain_into(usize::MAX, &mut v);
        v
    }

    async fn stop(self) -> Result<(), SensoryError> {
        self.shutdown.send(true).expect("send");
        tokio::time::timeout(Duration::from_secs(3), self.join)
            .await
            .expect("ingestor stops promptly after shutdown")
            .expect("join")
    }
}

fn test_config(port: u16) -> Config {
    Config {
        polygon_api_key: "test-key".into(),
        polygon_ws_url: format!("ws://127.0.0.1:{port}"),
        symbols: vec!["AAPL".into()],
        max_reconnect_attempts: 2,
        reconnect_base_ms: 30,
        reconnect_max_ms: 60,
        reconnect_stable_ms: 0,
        ws_connect_timeout_ms: 1_000,
        ws_handshake_timeout_ms: 1_000,
        ws_idle_timeout_ms: 150,
        ..Config::default()
    }
}

fn start_ingestor(config: Config) -> Harness {
    let questdb: SnapshotQueue = Arc::new(BoundedQueue::new(10_000));
    let redis: SnapshotQueue = Arc::new(BoundedQueue::new(10_000));
    let metrics = Arc::new(Metrics::default());
    let regime = Arc::new(RegimeCache::new(
        Duration::from_secs(15),
        SymbolFilter::from_symbols(&config.symbols),
    ));
    let mut ingestor = Ingestor::new(
        config,
        regime,
        Sinks {
            questdb: Arc::clone(&questdb),
            redis,
        },
        Arc::clone(&metrics),
        Arc::new(Health::new()),
    );
    let (shutdown, mut rx) = tokio::sync::watch::channel(false);
    let join = tokio::spawn(async move { ingestor.run(&mut rx).await });
    Harness {
        questdb,
        metrics,
        shutdown,
        join,
    }
}

async fn eventually(max: Duration, mut cond: impl FnMut() -> bool) -> bool {
    let end = Instant::now() + max;
    while Instant::now() < end {
        if cond() {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    cond()
}

#[tokio::test]
async fn ingestor_reconnect_survives_many_drops_with_backoff() {
    // max_reconnect_attempts is 2; the old code exited with code 1 on the
    // 5th lifetime disconnect. Six dropped sessions must not stop us.
    let server = start_server(|i| {
        let frames = vec![quote_frame(150.0, 150.05, now_ms())];
        behavior(frames, if i < 6 { After::Drop } else { After::Hold })
    })
    .await;
    // Sessions are never "stable" here, so the failure counter keeps growing
    // past max_reconnect_attempts (2): the spec says log CRITICAL and keep going.
    let config = Config {
        reconnect_stable_ms: 60_000,
        ..test_config(server.port)
    };
    let h = start_ingestor(config);

    assert!(
        eventually(Duration::from_secs(10), || server.connections() >= 7).await,
        "expected reconnects, saw {}",
        server.connections()
    );
    assert!(
        !h.join.is_finished(),
        "ingestor must keep running after repeated drops"
    );

    let times = server.accept_times();
    for pair in times.windows(2).take(6) {
        let gap = pair[1].duration_since(pair[0]);
        assert!(
            gap >= Duration::from_millis(20),
            "reconnect fired without backoff: {gap:?}"
        );
    }
    assert!(eventually(Duration::from_secs(3), || h.questdb.len() >= 7).await);
    assert!(Metrics::get(&h.metrics.reconnects) >= 6);
    h.stop().await.expect("clean shutdown");
}

#[tokio::test]
async fn backoff_resets_after_a_stable_session() {
    // Every session lives past reconnect_stable (0 ms), so each reconnect
    // starts again from the base delay instead of climbing to the cap.
    let server = start_server(|i| {
        let frames = vec![quote_frame(150.0, 150.05, now_ms())];
        behavior(frames, if i < 6 { After::Drop } else { After::Hold })
    })
    .await;
    let config = Config {
        reconnect_base_ms: 30,
        reconnect_max_ms: 2_000,
        reconnect_stable_ms: 0,
        ..test_config(server.port)
    };
    let h = start_ingestor(config);
    assert!(eventually(Duration::from_secs(10), || server.connections() >= 7).await);
    let times = server.accept_times();
    for pair in times.windows(2).take(6) {
        let gap = pair[1].duration_since(pair[0]);
        assert!(
            gap < Duration::from_millis(400),
            "backoff did not reset: {gap:?}"
        );
    }
    h.stop().await.expect("clean shutdown");
}

#[tokio::test]
async fn close_frame_reconnects_instead_of_exiting() {
    let server = start_server(|i| {
        behavior(
            vec![quote_frame(150.0, 150.05, now_ms())],
            if i == 0 { After::Close } else { After::Hold },
        )
    })
    .await;
    let h = start_ingestor(test_config(server.port));
    assert!(eventually(Duration::from_secs(5), || server.connections() >= 2).await);
    assert!(
        !h.join.is_finished(),
        "a Close frame used to end run() and exit 0"
    );
    h.stop().await.expect("clean shutdown");
}

#[tokio::test]
async fn auth_failed_is_fatal_and_reported() {
    let server = start_server(|_| Behavior {
        auth_ok: false,
        frames: vec![],
        after: After::Hold,
    })
    .await;
    let h = start_ingestor(test_config(server.port));
    let result = tokio::time::timeout(Duration::from_secs(5), h.join)
        .await
        .expect("returns")
        .expect("join");
    assert!(
        matches!(result, Err(SensoryError::AuthFailed { .. })),
        "{result:?}"
    );
    assert_eq!(
        server.connections(),
        1,
        "bad credentials must not be retried"
    );
}

#[tokio::test]
async fn malformed_frames_are_skipped_without_dropping_the_connection() {
    let server = start_server(|_| {
        behavior(
            vec![
                r#"{"ev":"status","status":"weird"}"#.into(), // object, not array
                "not json at all".into(),
                quote_frame(150.0, 150.05, now_ms()),
            ],
            After::Hold,
        )
    })
    .await;
    let h = start_ingestor(test_config(server.port));
    assert!(eventually(Duration::from_secs(5), || h.questdb.len() == 1).await);
    assert_eq!(server.connections(), 1, "no reconnect for a bad frame");
    assert!(Metrics::get(&h.metrics.bad_frames) >= 2);
    h.stop().await.expect("clean shutdown");
}

#[tokio::test]
async fn invalid_quotes_are_rejected_and_only_the_valid_one_emits() {
    let t = now_ms();
    let frames = vec![
        quote_frame(150.05, 150.0, t),                             // crossed
        quote_frame(-1.0, 150.0, t),                               // negative
        quote_frame(150.0, 150.05, 9_223_372_036_855),             // ms->ns overflow
        quote_frame(150.0, 150.05, 0),                             // no timestamp
        quote_frame(1e308, 1e308, t),                              // absurd price
        quote_frame(150.0, 150.05, t).replace("AAPL", "EVIL,x=1"), // hostile ticker
        quote_frame(150.0, 150.05, t).replace("AAPL", "MSFT"),     // not subscribed
        quote_frame(150.0, 150.05, t),                             // valid
    ];
    let server = start_server(move |_| behavior(frames.clone(), After::Hold)).await;
    let h = start_ingestor(test_config(server.port));
    assert!(
        eventually(Duration::from_secs(5), || Metrics::get(
            &h.metrics.rejected_messages
        ) >= 7)
        .await
    );
    let snaps = h.snapshots();
    assert_eq!(
        snaps.len(),
        1,
        "only the valid quote may produce a snapshot"
    );
    assert_eq!(snaps[0].symbol, "AAPL");
    h.stop().await.expect("clean shutdown");
}

#[tokio::test]
async fn idle_feed_is_pinged_then_dropped_and_reconnected() {
    let server =
        start_server(|i| behavior(vec![], if i == 0 { After::Silent } else { After::Hold })).await;
    let h = start_ingestor(test_config(server.port));
    // idle 150ms -> ping -> 150ms -> drop -> ~30ms backoff -> reconnect
    assert!(
        eventually(Duration::from_secs(4), || server.connections() >= 2).await,
        "watchdog must recycle a dead connection"
    );
    h.stop().await.expect("clean shutdown");
}

#[tokio::test]
async fn rolling_state_survives_a_reconnect() {
    let base = now_ms();
    let server = start_server(move |i| {
        // Quotes one second apart so each becomes its own 1s bar.
        let q = |k: i64| {
            quote_frame(
                100.0 + 0.01 * k as f64,
                100.05 + 0.01 * k as f64,
                base + k * 1_000,
            )
        };
        if i == 0 {
            behavior((0..4).map(q).collect(), After::Drop)
        } else {
            behavior((4..7).map(q).collect(), After::Hold)
        }
    })
    .await;
    let h = start_ingestor(test_config(server.port));
    assert!(eventually(Duration::from_secs(6), || h.questdb.len() == 7).await);
    let snaps = h.snapshots();
    assert!(
        !snaps[6].warmup,
        "7th quote spans two sessions; a recreated normalizer would still be warming up"
    );
    assert!(snaps[..5].iter().all(|s| s.warmup));
    h.stop().await.expect("clean shutdown");
}

#[tokio::test]
async fn shutdown_ends_a_live_session_cleanly() {
    let server =
        start_server(|_| behavior(vec![quote_frame(150.0, 150.05, now_ms())], After::Hold)).await;
    let h = start_ingestor(test_config(server.port));
    assert!(eventually(Duration::from_secs(5), || h.questdb.len() == 1).await);
    let started = Instant::now();
    h.stop().await.expect("clean shutdown");
    assert!(started.elapsed() < Duration::from_secs(2));
}
