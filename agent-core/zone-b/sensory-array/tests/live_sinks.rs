//! Tests against REAL Redis / QuestDB. `#[ignore]`d: they need external
//! services and are skipped by a plain `cargo test`. Run with:
//!
//! ```text
//! REDIS_TEST_URL=redis://redis:6379 QUESTDB_TEST_HOST=questdb \
//!   cargo test --test live_sinks -- --ignored --test-threads=1
//! ```
//! (`QUESTDB_TEST_ILP_PORT` / `QUESTDB_TEST_HTTP_PORT` default to 9009 / 9000.)

use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::StreamExt;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

use sensory_array::metrics::Metrics;
use sensory_array::publisher::{run_publisher, SNAPSHOT_CHANNEL};
use sensory_array::questdb_writer::{run_writer, SecureIlp, SnapshotQueue, WriterSettings};
use sensory_array::queue::BoundedQueue;
use sensory_array::regime::{run_regime_subscriber, RegimeCache, REGIME_CHANNEL};
use sensory_array::runtime::unix_ns;
use sensory_array::testutil::sample_snapshot;
use sensory_array::validate::SymbolFilter;

fn env(key: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| panic!("{key} must be set for live tests"))
}

async fn eventually<F: FnMut() -> bool>(max: Duration, mut cond: F) -> bool {
    let end = Instant::now() + max;
    while Instant::now() < end {
        if cond() {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    cond()
}

async fn redis_conn(url: &str) -> redis::aio::MultiplexedConnection {
    redis::Client::open(url)
        .expect("client")
        .get_multiplexed_async_connection()
        .await
        .expect("connect")
}

async fn kill_clients(url: &str, kind: &str) {
    let mut c = redis_conn(url).await;
    let _: i64 = redis::cmd("CLIENT")
        .arg("KILL")
        .arg("TYPE")
        .arg(kind)
        .query_async(&mut c)
        .await
        .expect("client kill");
}

#[tokio::test]
#[ignore = "needs REDIS_TEST_URL"]
async fn publisher_delivers_and_survives_its_connection_being_killed() {
    let url = env("REDIS_TEST_URL");
    let client = redis::Client::open(url.as_str()).expect("client");
    let mut sub = client.get_async_pubsub().await.expect("pubsub");
    sub.subscribe(SNAPSHOT_CHANNEL).await.expect("subscribe");
    let mut stream = sub.on_message();

    let queue: SnapshotQueue = Arc::new(BoundedQueue::new(1024));
    let metrics = Arc::new(Metrics::default());
    let (tx, rx) = tokio::sync::watch::channel(false);
    let task = tokio::spawn(run_publisher(
        url.clone(),
        Arc::clone(&queue),
        Arc::clone(&metrics),
        rx,
    ));

    let ts = unix_ns();
    queue.push(Arc::new(sample_snapshot("AAPL", ts)));
    let msg = tokio::time::timeout(Duration::from_secs(5), stream.next())
        .await
        .expect("timeout")
        .expect("message");
    let payload: String = msg.get_payload().expect("payload");
    let v: serde_json::Value = serde_json::from_str(&payload).expect("json");
    assert_eq!(v["symbol"], "AAPL");
    assert_eq!(v["ingestion_ts_ns"], ts);

    // Kill every normal client connection (including the publisher's); the
    // old multiplexed connection never recovered from this.
    kill_clients(&url, "normal").await;
    tokio::time::sleep(Duration::from_millis(300)).await;
    let mut got_after_kill = false;
    for i in 0..20 {
        queue.push(Arc::new(sample_snapshot("MSFT", ts + 1 + i)));
        if let Ok(Some(m)) = tokio::time::timeout(Duration::from_millis(500), stream.next()).await {
            let p: String = m.get_payload().expect("payload");
            if p.contains("MSFT") {
                got_after_kill = true;
                break;
            }
        }
    }
    assert!(
        got_after_kill,
        "publisher must recover after Redis dropped its connection"
    );
    assert!(Metrics::get(&metrics.redis_published) >= 2);
    tx.send(true).expect("send");
    task.await.expect("join");
}

#[tokio::test]
#[ignore = "needs REDIS_TEST_URL"]
async fn regime_subscriber_receives_and_reconnects_after_kill() {
    let url = env("REDIS_TEST_URL");
    let cache = Arc::new(RegimeCache::new(
        Duration::from_secs(15),
        SymbolFilter::from_symbols(&["AAPL".into()]),
    ));
    let metrics = Arc::new(Metrics::default());
    let (tx, rx) = tokio::sync::watch::channel(false);
    let task = tokio::spawn(run_regime_subscriber(
        url.clone(),
        Arc::clone(&cache),
        Arc::clone(&metrics),
        rx,
    ));
    let mut publisher = redis_conn(&url).await;
    let publish = |label: &str| {
        format!(
            r#"{{"symbol":"AAPL","label":"{label}","confidence":0.8,"ts_ns":{}}}"#,
            unix_ns()
        )
    };

    // Retry publishing until the subscriber is listening.
    let mut ok = false;
    for _ in 0..40 {
        let _: i64 = redis::cmd("PUBLISH")
            .arg(REGIME_CHANNEL)
            .arg(publish("TRENDING_BULL"))
            .query_async(&mut publisher)
            .await
            .expect("publish");
        if cache.lookup("AAPL", unix_ns()).0 == "TRENDING_BULL" {
            ok = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(ok, "label must arrive");

    // Drop the subscriber's connection; it must reconnect and keep updating.
    kill_clients(&url, "pubsub").await;
    let mut recovered = false;
    for _ in 0..80 {
        let _: i64 = redis::cmd("PUBLISH")
            .arg(REGIME_CHANNEL)
            .arg(publish("CRISIS"))
            .query_async(&mut publisher)
            .await
            .expect("publish");
        if cache.lookup("AAPL", unix_ns()).0 == "CRISIS" {
            recovered = true;
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    assert!(
        recovered,
        "subscriber must reconnect after its connection was killed"
    );

    // Hostile payloads are rejected, not cached.
    let _: i64 = redis::cmd("PUBLISH")
        .arg(REGIME_CHANNEL)
        .arg(r#"{"symbol":"AAPL\nX","label":"CRISIS","confidence":0.5}"#)
        .query_async(&mut publisher)
        .await
        .expect("publish");
    assert!(
        eventually(Duration::from_secs(3), || Metrics::get(
            &metrics.regime_rejected
        ) >= 1)
        .await
    );
    tx.send(true).expect("send");
    task.await.expect("join");
}

fn urlencode(q: &str) -> String {
    q.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'*' | b'(' | b')' | b',' | b'.' => {
                (b as char).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

async fn questdb_query(host: &str, http_port: u16, sql: &str) -> Option<serde_json::Value> {
    let mut s = TcpStream::connect((host, http_port)).await.ok()?;
    let req = format!(
        "GET /exec?query={} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n",
        urlencode(sql)
    );
    s.write_all(req.as_bytes()).await.ok()?;
    let mut buf = Vec::new();
    // Bounded: a keep-alive server must not hang the test.
    let _ = tokio::time::timeout(Duration::from_secs(5), s.read_to_end(&mut buf)).await;
    let text = String::from_utf8_lossy(&buf).into_owned();
    let (start, end) = (text.find('{')?, text.rfind('}')?);
    serde_json::from_str(&text[start..=end]).ok()
}

#[tokio::test]
#[ignore = "needs QUESTDB_TEST_HOST"]
async fn questdb_receives_spec_schema_rows() {
    let host = env("QUESTDB_TEST_HOST");
    let ilp: u16 = std::env::var("QUESTDB_TEST_ILP_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(9009);
    let http: u16 = std::env::var("QUESTDB_TEST_HTTP_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(9000);

    let queue: SnapshotQueue = Arc::new(BoundedQueue::new(1024));
    let metrics = Arc::new(Metrics::default());
    let (tx, rx) = tokio::sync::watch::channel(false);
    let settings = WriterSettings {
        host: host.clone(),
        port: ilp,
        connect_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        linger: Duration::from_millis(10),
        batch_max: 256,
        backoff_base: Duration::from_millis(100),
        backoff_max: Duration::from_secs(2),
        secure: SecureIlp::default(),
    };
    let task = tokio::spawn(run_writer(
        settings,
        Arc::clone(&queue),
        Arc::clone(&metrics),
        rx,
    ));

    let base = unix_ns();
    for i in 0..50 {
        queue.push(Arc::new(sample_snapshot("LIVETEST", base + i)));
    }
    let mut count = 0_i64;
    let deadline = Instant::now() + Duration::from_secs(20);
    while count < 50 && Instant::now() < deadline {
        let sql = "select count() from market_data where symbol = 'LIVETEST'";
        if let Some(v) = questdb_query(&host, http, sql).await {
            count = v["dataset"][0][0].as_i64().unwrap_or(0);
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    let seen = count >= 50;
    assert!(seen, "QuestDB must contain the 50 rows (saw {count})");

    let cols = questdb_query(&host, http, "select * from market_data limit 1")
        .await
        .expect("query");
    let names: Vec<String> = cols["columns"]
        .as_array()
        .expect("columns")
        .iter()
        .map(|c| c["name"].as_str().unwrap_or("").to_string())
        .collect();
    for want in [
        "symbol",
        "ingestion_ts",
        "exchange_ts",
        "mid_price",
        "ofi",
        "realized_vol",
        "is_stale",
        "warmup",
    ] {
        assert!(
            names.iter().any(|n| n == want),
            "column {want} missing in {names:?}"
        );
    }
    tx.send(true).expect("send");
    task.await.expect("join");
    assert_eq!(Metrics::get(&metrics.ilp_invalid), 0);
}
