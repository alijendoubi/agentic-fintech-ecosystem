//! Sensory Array binary: wiring, signal handling and graceful shutdown.
//!
//! `sensory-array --healthcheck` is the container HEALTHCHECK entry point.

use std::io::Write;
use std::path::Path;
use std::process::ExitCode;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::watch;
use tokio::task::JoinHandle;
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

use sensory_array::config::Config;
use sensory_array::health::{check_file, run_heartbeat, Health, HEALTHCHECK_MAX_AGE_S};
use sensory_array::ingestor::{Ingestor, Sinks};
use sensory_array::metrics::Metrics;
use sensory_array::publisher::run_publisher;
use sensory_array::questdb_writer::{run_writer, SnapshotQueue, WriterSettings};
use sensory_array::queue::BoundedQueue;
use sensory_array::regime::{run_regime_subscriber, RegimeCache};
use sensory_array::runtime::unix_ns;
use sensory_array::validate::SymbolFilter;

const SHUTDOWN_GRACE: Duration = Duration::from_secs(10);
const METRICS_EVERY: Duration = Duration::from_secs(30);
const DEFAULT_HEALTH_FILE: &str = "/tmp/sensory-array.health";

#[tokio::main]
async fn main() -> ExitCode {
    if std::env::args().any(|a| a == "--healthcheck") {
        return healthcheck();
    }
    init_tracing();
    match Config::from_env() {
        Ok(config) => run(config).await,
        Err(e) => {
            error!(error = %e, "invalid configuration");
            ExitCode::from(2)
        }
    }
}

fn init_tracing() {
    let level = std::env::var("LOG_LEVEL").unwrap_or_else(|_| "info".to_string());
    let filter = EnvFilter::try_from_default_env()
        .or_else(|_| EnvFilter::try_new(&level))
        .unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .json()
        .init();
}

fn healthcheck() -> ExitCode {
    let path = std::env::var("HEALTH_FILE").unwrap_or_else(|_| DEFAULT_HEALTH_FILE.to_string());
    match check_file(
        Path::new(&path),
        unix_ns() / 1_000_000_000,
        HEALTHCHECK_MAX_AGE_S,
    ) {
        Ok(()) => ExitCode::SUCCESS,
        Err(reason) => {
            let _ = writeln!(std::io::stderr(), "unhealthy: {reason}");
            ExitCode::FAILURE
        }
    }
}

struct Tasks {
    handles: Vec<JoinHandle<()>>,
}

async fn run(config: Config) -> ExitCode {
    info!(config = ?config, "Sensory Array starting");

    let metrics = Arc::new(Metrics::default());
    let health = Arc::new(Health::new());
    let regime = Arc::new(RegimeCache::new(
        Duration::from_secs(config.regime_max_age_s),
        SymbolFilter::from_symbols(&config.symbols),
    ));
    let questdb_queue: SnapshotQueue = Arc::new(BoundedQueue::new(config.questdb_queue_capacity));
    let redis_queue: SnapshotQueue = Arc::new(BoundedQueue::new(config.redis_queue_capacity));
    let (shutdown_tx, shutdown_rx) = watch::channel(false);
    let shutdown_tx = Arc::new(shutdown_tx);

    let tasks = spawn_tasks(
        &config,
        &metrics,
        &health,
        &regime,
        &questdb_queue,
        &redis_queue,
        &shutdown_rx,
    );
    tokio::spawn(signal_listener(Arc::clone(&shutdown_tx)));

    let sinks = Sinks {
        questdb: Arc::clone(&questdb_queue),
        redis: Arc::clone(&redis_queue),
    };
    let mut ingestor = Ingestor::new(config, regime, sinks, Arc::clone(&metrics), health);
    let mut ingest_shutdown = shutdown_rx.clone();
    let result = ingestor.run(&mut ingest_shutdown).await;

    // Ingestor finished (shutdown or fatal): stop everything else cleanly.
    let _ = shutdown_tx.send(true);
    tasks.join(SHUTDOWN_GRACE).await;
    metrics.log_summary(questdb_queue.dropped(), redis_queue.dropped());

    match result {
        Ok(()) => {
            info!("Sensory Array shutdown complete");
            ExitCode::SUCCESS
        }
        Err(e) => {
            error!(error = %e, "Sensory Array exiting with error");
            ExitCode::FAILURE
        }
    }
}

fn spawn_tasks(
    config: &Config,
    metrics: &Arc<Metrics>,
    health: &Arc<Health>,
    regime: &Arc<RegimeCache>,
    questdb_queue: &SnapshotQueue,
    redis_queue: &SnapshotQueue,
    shutdown: &watch::Receiver<bool>,
) -> Tasks {
    let alive_window = config.ws_idle_timeout() * 2 + Duration::from_secs(10);
    let handles = vec![
        tokio::spawn(run_regime_subscriber(
            config.redis_url.clone(),
            Arc::clone(regime),
            Arc::clone(metrics),
            shutdown.clone(),
        )),
        tokio::spawn(run_writer(
            WriterSettings::from_config(config),
            Arc::clone(questdb_queue),
            Arc::clone(metrics),
            shutdown.clone(),
        )),
        tokio::spawn(run_publisher(
            config.redis_url.clone(),
            Arc::clone(redis_queue),
            Arc::clone(metrics),
            shutdown.clone(),
        )),
        tokio::spawn(run_heartbeat(
            config.health_file.clone(),
            Arc::clone(health),
            alive_window,
            shutdown.clone(),
        )),
        tokio::spawn(metrics_logger(
            Arc::clone(metrics),
            Arc::clone(questdb_queue),
            Arc::clone(redis_queue),
            shutdown.clone(),
        )),
    ];
    Tasks { handles }
}

impl Tasks {
    /// Wait for every task, at most `grace` in total.
    async fn join(self, grace: Duration) {
        let all = async {
            for h in self.handles {
                if let Err(e) = h.await {
                    error!(error = %e, "background task ended abnormally");
                }
            }
        };
        if tokio::time::timeout(grace, all).await.is_err() {
            warn!("background tasks did not stop within the grace period");
        }
    }
}

async fn metrics_logger(
    metrics: Arc<Metrics>,
    questdb: SnapshotQueue,
    redis: SnapshotQueue,
    mut shutdown: watch::Receiver<bool>,
) {
    loop {
        tokio::select! {
            () = tokio::time::sleep(METRICS_EVERY) => {
                metrics.log_summary(questdb.dropped(), redis.dropped());
            }
            () = sensory_array::runtime::wait_shutdown(&mut shutdown) => return,
        }
    }
}

/// Wait for SIGTERM (`docker stop`) or SIGINT / Ctrl-C, then request shutdown.
async fn signal_listener(shutdown_tx: Arc<watch::Sender<bool>>) {
    wait_for_termination_signal().await;
    info!("termination signal received; shutting down");
    let _ = shutdown_tx.send(true);
}

#[cfg(unix)]
async fn wait_for_termination_signal() {
    use tokio::signal::unix::{signal, SignalKind};
    let (term, int) = (
        signal(SignalKind::terminate()),
        signal(SignalKind::interrupt()),
    );
    match (term, int) {
        (Ok(mut term), Ok(mut int)) => {
            tokio::select! {
                _ = term.recv() => {}
                _ = int.recv() => {}
            }
        }
        (a, b) => {
            error!(
                term_ok = a.is_ok(),
                int_ok = b.is_ok(),
                "cannot install signal handlers; falling back to Ctrl-C"
            );
            wait_ctrl_c().await;
        }
    }
}

#[cfg(not(unix))]
async fn wait_for_termination_signal() {
    wait_ctrl_c().await;
}

async fn wait_ctrl_c() {
    if let Err(e) = tokio::signal::ctrl_c().await {
        // Never treat "cannot listen" as a shutdown request.
        error!(error = %e, "cannot listen for Ctrl-C; signals will be ignored");
        std::future::pending::<()>().await;
    }
}
