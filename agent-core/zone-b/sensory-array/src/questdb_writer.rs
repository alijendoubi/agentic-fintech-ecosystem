//! Async QuestDB ILP writer.
//!
//! Runs as its own task, fed by a bounded drop-oldest queue, so a QuestDB
//! outage can never stall the WebSocket read loop:
//!
//! * snapshots are batched (`linger` / `batch_max`) into one buffer and one
//!   write + flush per batch (the spec's buffer + flush cycle);
//! * connecting has a timeout and reconnects back off exponentially with
//!   jitter; while disconnected the queue keeps only the newest items;
//! * a failed write tears the connection down and re-queues exactly the lines
//!   that were not completely written, so a broken connection never leaves a
//!   half line followed by a retried copy (QuestDB discards an unterminated
//!   trailing line when the connection closes). Lines that were fully handed
//!   to the kernel before the failure may be delivered twice; use
//!   `DEDUP UPSERT KEYS(timestamp, symbol)` on the table if exactly-once rows
//!   matter.
//!
//! ILP is one-way: the server reports parse errors on the socket, which we do
//! not read. Rows QuestDB rejects therefore surface in QuestDB's log, not here.

use std::sync::Arc;
use std::time::Duration;

use tokio::io::{AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::time::timeout;
use tracing::{debug, info, warn};

use crate::error::{Result, SensoryError};
use crate::ilp::build_ilp_line;
use crate::metrics::Metrics;
use crate::normalizer::MarketSnapshot;
use crate::queue::BoundedQueue;
use crate::runtime::{jittered_backoff, sleep_or_shutdown, wait_shutdown, Shutdown};

pub type SnapshotQueue = Arc<BoundedQueue<Arc<MarketSnapshot>>>;
type Conn = Box<dyn AsyncWrite + Unpin + Send>;

/// Optional TLS + ECDSA-auth settings (need the `ilp-secure` cargo feature).
#[derive(Debug, Clone, Default)]
pub struct SecureIlp {
    pub tls: bool,
    pub key_id: Option<String>,
    pub token: Option<String>,
    pub ca_file: Option<String>,
}

impl SecureIlp {
    pub fn enabled(&self) -> bool {
        self.tls || self.key_id.is_some()
    }
}

#[derive(Debug, Clone)]
pub struct WriterSettings {
    pub host: String,
    pub port: u16,
    pub connect_timeout: Duration,
    pub write_timeout: Duration,
    /// After the first queued item, wait this long for more before writing.
    pub linger: Duration,
    pub batch_max: usize,
    pub backoff_base: Duration,
    pub backoff_max: Duration,
    pub secure: SecureIlp,
}

impl WriterSettings {
    pub fn from_config(c: &crate::config::Config) -> Self {
        Self {
            host: c.questdb_ilp_host.clone(),
            port: c.questdb_ilp_port,
            connect_timeout: Duration::from_millis(c.questdb_connect_timeout_ms),
            write_timeout: Duration::from_millis(c.questdb_write_timeout_ms),
            linger: Duration::from_millis(c.questdb_flush_interval_ms),
            batch_max: 512,
            backoff_base: Duration::from_millis(100),
            backoff_max: Duration::from_secs(5),
            secure: SecureIlp {
                tls: c.questdb_tls,
                key_id: c.questdb_auth_key_id.clone(),
                token: c.questdb_auth_token.clone(),
                ca_file: c.questdb_tls_ca_file.clone(),
            },
        }
    }
}

#[cfg(feature = "ilp-secure")]
async fn wrap_secure(stream: TcpStream, s: &WriterSettings) -> Result<Conn> {
    if !s.secure.enabled() {
        return Ok(Box::new(stream));
    }
    crate::ilp_secure::secure_connect(stream, &s.host, &s.secure).await
}

#[cfg(not(feature = "ilp-secure"))]
async fn wrap_secure(stream: TcpStream, s: &WriterSettings) -> Result<Conn> {
    if s.secure.enabled() {
        return Err(SensoryError::Config {
            msg: "secure ILP requested but built without the `ilp-secure` feature".into(),
        });
    }
    Ok(Box::new(stream))
}

/// Open a connection (TCP, then optional TLS/auth) within `connect_timeout`.
async fn open_connection(s: &WriterSettings) -> Result<Conn> {
    let addr = format!("{}:{}", s.host, s.port);
    let stream = timeout(s.connect_timeout, TcpStream::connect(&addr))
        .await
        .map_err(|_| SensoryError::session(format!("QuestDB connect to {addr} timed out")))??;
    stream.set_nodelay(true)?;
    timeout(s.connect_timeout, wrap_secure(stream, s))
        .await
        .map_err(|_| {
            SensoryError::session(format!("QuestDB secure handshake with {addr} timed out"))
        })?
}

/// Encoded batch: the bytes, the end offset of every line and the snapshots
/// those lines came from (invalid snapshots are skipped and counted).
struct Encoded {
    buf: Vec<u8>,
    line_ends: Vec<usize>,
    items: Vec<Arc<MarketSnapshot>>,
}

fn encode_batch(batch: &[Arc<MarketSnapshot>], metrics: &Metrics) -> Encoded {
    let mut enc = Encoded {
        buf: Vec::with_capacity(batch.len() * 400),
        line_ends: Vec::with_capacity(batch.len()),
        items: Vec::with_capacity(batch.len()),
    };
    for snap in batch {
        match build_ilp_line(snap) {
            Ok(line) => {
                enc.buf.extend_from_slice(line.as_bytes());
                enc.line_ends.push(enc.buf.len());
                enc.items.push(Arc::clone(snap));
            }
            Err(e) => {
                Metrics::inc(&metrics.ilp_invalid);
                debug!(symbol = %snap.symbol, error = %e, "snapshot not encodable as ILP; dropped");
            }
        }
    }
    enc
}

/// How many whole lines are covered by the first `written` bytes.
fn fully_written_lines(line_ends: &[usize], written: usize) -> usize {
    line_ends.partition_point(|end| *end <= written)
}

/// Write all of `buf`, reporting how many bytes were accepted even on failure.
async fn write_tracked(conn: &mut Conn, buf: &[u8], per_write: Duration) -> (usize, Result<()>) {
    let mut written = 0;
    while written < buf.len() {
        match timeout(per_write, conn.write(&buf[written..])).await {
            Ok(Ok(0)) => return (written, Err(SensoryError::session("QuestDB wrote 0 bytes"))),
            Ok(Ok(n)) => written += n,
            Ok(Err(e)) => return (written, Err(e.into())),
            Err(_) => {
                return (
                    written,
                    Err(SensoryError::session("QuestDB write timed out")),
                )
            }
        }
    }
    match timeout(per_write, conn.flush()).await {
        Ok(Ok(())) => (written, Ok(())),
        Ok(Err(e)) => (written, Err(e.into())),
        Err(_) => (
            written,
            Err(SensoryError::session("QuestDB flush timed out")),
        ),
    }
}

struct WriterState {
    conn: Option<Conn>,
    failures: u32,
}

/// Send one batch. Returns `true` if shutdown was observed while backing off.
async fn send_batch(
    s: &WriterSettings,
    st: &mut WriterState,
    queue: &BoundedQueue<Arc<MarketSnapshot>>,
    metrics: &Metrics,
    batch: &[Arc<MarketSnapshot>],
    shutdown: &mut Shutdown,
) -> bool {
    let enc = encode_batch(batch, metrics);
    if enc.items.is_empty() {
        return false;
    }
    if st.conn.is_none() {
        match open_connection(s).await {
            Ok(c) => {
                info!(host = %s.host, port = s.port, "QuestDB ILP connected");
                st.conn = Some(c);
            }
            Err(e) => return fail_and_backoff(s, st, queue, enc.items, &e, shutdown).await,
        }
    }
    let Some(conn) = st.conn.as_mut() else {
        return false;
    };
    let (written, result) = write_tracked(conn, &enc.buf, s.write_timeout).await;
    match result {
        Ok(()) => {
            Metrics::add(&metrics.ilp_written, enc.items.len() as u64);
            st.failures = 0;
            false
        }
        Err(e) => {
            Metrics::inc(&metrics.ilp_write_errors);
            let sent = fully_written_lines(&enc.line_ends, written);
            Metrics::add(&metrics.ilp_written, sent as u64);
            let rest: Vec<_> = enc.items.into_iter().skip(sent).collect();
            fail_and_backoff(s, st, queue, rest, &e, shutdown).await
        }
    }
}

async fn fail_and_backoff(
    s: &WriterSettings,
    st: &mut WriterState,
    queue: &BoundedQueue<Arc<MarketSnapshot>>,
    unsent: Vec<Arc<MarketSnapshot>>,
    err: &SensoryError,
    shutdown: &mut Shutdown,
) -> bool {
    st.conn = None;
    st.failures = st.failures.saturating_add(1);
    queue.requeue_front(unsent);
    let delay = jittered_backoff(s.backoff_base, s.backoff_max, st.failures);
    warn!(failures = st.failures, retry_in_ms = delay.as_millis() as u64, error = %err, "QuestDB unavailable; buffering");
    sleep_or_shutdown(delay, shutdown).await
}

/// Writer task. Returns after `shutdown`, having made one bounded attempt to
/// flush what is still queued.
pub async fn run_writer(
    settings: WriterSettings,
    queue: SnapshotQueue,
    metrics: Arc<Metrics>,
    mut shutdown: Shutdown,
) {
    let mut st = WriterState {
        conn: None,
        failures: 0,
    };
    let mut batch: Vec<Arc<MarketSnapshot>> = Vec::with_capacity(settings.batch_max);
    loop {
        batch.clear();
        tokio::select! {
            () = queue.pop_batch_lingering(settings.batch_max, settings.linger, &mut batch) => {}
            () = wait_shutdown(&mut shutdown) => break,
        }
        if send_batch(&settings, &mut st, &queue, &metrics, &batch, &mut shutdown).await {
            break;
        }
    }
    final_flush(&settings, &mut st, &queue, &metrics, batch).await;
    info!("QuestDB writer stopped");
}

/// Best-effort last flush on shutdown (single attempt, bounded by timeouts).
async fn final_flush(
    s: &WriterSettings,
    st: &mut WriterState,
    queue: &BoundedQueue<Arc<MarketSnapshot>>,
    metrics: &Metrics,
    mut pending: Vec<Arc<MarketSnapshot>>,
) {
    queue.drain_into(usize::MAX, &mut pending);
    if pending.is_empty() {
        return;
    }
    let (tx, mut rx) = tokio::sync::watch::channel(true);
    let outcome = timeout(
        s.connect_timeout + s.write_timeout,
        send_batch(s, st, queue, metrics, &pending, &mut rx),
    )
    .await;
    drop(tx);
    if outcome.is_err() {
        warn!(lost = pending.len(), "QuestDB final flush timed out");
    }
    if let Some(mut c) = st.conn.take() {
        let _ = timeout(Duration::from_millis(500), c.shutdown()).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::sample_snapshot;
    use tokio::io::AsyncReadExt;
    use tokio::net::TcpListener;

    fn settings(port: u16) -> WriterSettings {
        WriterSettings {
            host: "127.0.0.1".into(),
            port,
            connect_timeout: Duration::from_millis(300),
            write_timeout: Duration::from_millis(300),
            linger: Duration::from_millis(5),
            batch_max: 64,
            backoff_base: Duration::from_millis(20),
            backoff_max: Duration::from_millis(100),
            secure: SecureIlp::default(),
        }
    }

    fn snap(sym: &str, i: i64) -> Arc<MarketSnapshot> {
        Arc::new(sample_snapshot(sym, 1_700_000_000_000_000_000 + i))
    }

    async fn read_lines(listener: &TcpListener, want: usize, max: Duration) -> Vec<String> {
        let (mut sock, _) = timeout(max, listener.accept())
            .await
            .expect("accept")
            .expect("io");
        let mut acc = String::new();
        let mut buf = [0_u8; 8192];
        let deadline = tokio::time::Instant::now() + max;
        while acc.matches('\n').count() < want && tokio::time::Instant::now() < deadline {
            if let Ok(Ok(n)) = timeout(Duration::from_millis(100), sock.read(&mut buf)).await {
                if n == 0 {
                    break;
                }
                acc.push_str(&String::from_utf8_lossy(&buf[..n]));
            }
        }
        acc.lines().map(str::to_string).collect()
    }

    #[test]
    fn fully_written_lines_counts_only_complete_lines() {
        let ends = [10, 25, 40];
        assert_eq!(fully_written_lines(&ends, 0), 0);
        assert_eq!(fully_written_lines(&ends, 9), 0);
        assert_eq!(fully_written_lines(&ends, 10), 1);
        assert_eq!(fully_written_lines(&ends, 24), 1);
        assert_eq!(fully_written_lines(&ends, 40), 3);
    }

    #[test]
    fn encode_batch_skips_and_counts_invalid_snapshots() {
        let metrics = Metrics::default();
        let mut bad = sample_snapshot("AAPL", 1_700_000_000_000_000_001);
        bad.z_score = f64::NAN;
        let mut evil = sample_snapshot("AAPL", 1_700_000_000_000_000_002);
        evil.symbol = "X\nEVIL".into();
        let batch = vec![
            snap("AAPL", 3),
            Arc::new(bad),
            Arc::new(evil),
            snap("MSFT", 4),
        ];
        let enc = encode_batch(&batch, &metrics);
        assert_eq!(enc.items.len(), 2);
        assert_eq!(enc.line_ends.len(), 2);
        assert_eq!(Metrics::get(&metrics.ilp_invalid), 2);
        assert_eq!(*enc.line_ends.last().expect("end"), enc.buf.len());
        assert_eq!(enc.buf.iter().filter(|b| **b == b'\n').count(), 2);
    }

    #[tokio::test]
    async fn batches_are_written_as_valid_ilp_lines() {
        let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let port = listener.local_addr().expect("addr").port();
        let queue: SnapshotQueue = Arc::new(BoundedQueue::new(1024));
        let metrics = Arc::new(Metrics::default());
        let (tx, rx) = tokio::sync::watch::channel(false);
        let task = tokio::spawn(run_writer(
            settings(port),
            Arc::clone(&queue),
            Arc::clone(&metrics),
            rx,
        ));
        for i in 0..5 {
            queue.push(snap("AAPL", i));
        }
        let lines = read_lines(&listener, 5, Duration::from_secs(3)).await;
        assert_eq!(lines.len(), 5);
        assert!(lines
            .iter()
            .all(|l| l.starts_with("market_data,symbol=AAPL ")));
        assert_eq!(
            lines[0],
            build_ilp_line(&snap("AAPL", 0)).expect("ilp").trim_end()
        );
        tx.send(true).expect("send");
        timeout(Duration::from_secs(3), task)
            .await
            .expect("stops")
            .expect("join");
        assert_eq!(Metrics::get(&metrics.ilp_written), 5);
    }

    #[tokio::test]
    async fn outage_never_blocks_producers_and_keeps_newest_then_recovers() {
        // Reserve a port, then close it: connections are refused (outage).
        let probe = TcpListener::bind("127.0.0.1:0").await.expect("bind");
        let port = probe.local_addr().expect("addr").port();
        drop(probe);

        let queue: SnapshotQueue = Arc::new(BoundedQueue::new(20));
        let metrics = Arc::new(Metrics::default());
        let (tx, rx) = tokio::sync::watch::channel(false);
        let task = tokio::spawn(run_writer(
            settings(port),
            Arc::clone(&queue),
            Arc::clone(&metrics),
            rx,
        ));

        let started = std::time::Instant::now();
        for i in 0..1_000 {
            queue.push(snap("AAPL", i));
        }
        assert!(
            started.elapsed() < Duration::from_millis(500),
            "producer must not block"
        );
        tokio::time::sleep(Duration::from_millis(150)).await; // several failed connects

        // QuestDB comes back on the same port.
        let listener = TcpListener::bind(("127.0.0.1", port))
            .await
            .expect("rebind");
        queue.push(snap("MSFT", 5_000));
        let lines = read_lines(&listener, 1, Duration::from_secs(5)).await;
        assert!(!lines.is_empty(), "writer must reconnect on its own");
        assert!(
            queue.dropped() >= 980,
            "old items were dropped, not buffered forever"
        );
        tx.send(true).expect("send");
        timeout(Duration::from_secs(3), task)
            .await
            .expect("stops")
            .expect("join");
    }

    #[tokio::test]
    async fn connect_timeout_is_bounded() {
        // TEST-NET-1 is unroutable: either an immediate error or a timeout,
        // but never an unbounded hang.
        let mut s = settings(9009);
        s.host = "192.0.2.1".into();
        s.connect_timeout = Duration::from_millis(100);
        let t = std::time::Instant::now();
        assert!(open_connection(&s).await.is_err());
        assert!(t.elapsed() < Duration::from_secs(2));
    }

    #[tokio::test]
    async fn secure_requested_without_feature_fails_closed() {
        #[cfg(not(feature = "ilp-secure"))]
        {
            let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
            let mut s = settings(listener.local_addr().expect("addr").port());
            s.secure.tls = true;
            assert!(open_connection(&s).await.is_err());
        }
    }
}
