mod config;
mod error;
mod ingestor;
mod normalizer;
mod questdb_writer;
mod ttl;

use std::collections::HashMap;
use std::sync::Arc;

use futures_util::StreamExt;
use tokio::sync::RwLock;
use tracing::{error, info};
use tracing_subscriber::EnvFilter;

use crate::config::Config;
use crate::ingestor::{Ingestor, RegimeCache, RegimeEntry};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let config = Config::from_env().map_err(|e| anyhow::anyhow!("{e}"))?;

    // Structured JSON logging
    tracing_subscriber::fmt()
        .with_env_filter(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new(&config.log_level)),
        )
        .json()
        .init();

    info!(
        symbols = ?config.symbols,
        questdb_host = %config.questdb_ilp_host,
        "Sensory Array starting"
    );

    // Shared regime label cache — populated by subscriber, read by ingestor
    let regime_cache: RegimeCache = Arc::new(RwLock::new(HashMap::new()));

    // Spawn regime label subscriber (Zone A → Redis → Zone B hot path)
    let regime_cache_sub = Arc::clone(&regime_cache);
    let redis_url = config.redis_url.clone();
    tokio::spawn(async move {
        if let Err(e) = run_regime_subscriber(redis_url, regime_cache_sub).await {
            error!("Regime subscriber failed: {e}");
        }
    });

    // Run main ingestor with reconnect loop
    let ingestor = Ingestor::new(config, Arc::clone(&regime_cache));

    // Graceful shutdown on SIGTERM / SIGINT
    tokio::select! {
        result = ingestor.run() => {
            if let Err(e) = result {
                error!("Ingestor exited with error: {e}");
                std::process::exit(1);
            }
        }
        _ = tokio::signal::ctrl_c() => {
            info!("Received SIGINT, shutting down");
        }
    }

    info!("Sensory Array shutdown complete");
    Ok(())
}

/// Subscribe to `regime:labels` Redis channel.
/// Updates the shared RegimeCache whenever a new label arrives from the Python HMM detector.
async fn run_regime_subscriber(
    redis_url: String,
    cache: RegimeCache,
) -> anyhow::Result<()> {
    let client = redis::Client::open(redis_url.as_str())?;
    let mut pubsub = client.get_async_pubsub().await?;
    pubsub.subscribe("regime:labels").await?;

    info!("Regime subscriber listening on Redis 'regime:labels'");

    let mut stream = pubsub.on_message();
    while let Some(msg) = stream.next().await {
        let payload: String = msg.get_payload().unwrap_or_default();
        match serde_json::from_str::<serde_json::Value>(&payload) {
            Ok(v) => {
                let symbol = v
                    .get("symbol")
                    .and_then(|s| s.as_str())
                    .unwrap_or("")
                    .to_string();
                let label = v
                    .get("label")
                    .and_then(|l| l.as_str())
                    .unwrap_or("REGIME_UNKNOWN")
                    .to_string();
                let confidence = v
                    .get("confidence")
                    .and_then(|c| c.as_f64())
                    .unwrap_or(0.0);

                if !symbol.is_empty() {
                    let mut w = cache.write().await;
                    w.insert(symbol, RegimeEntry { label, confidence });
                }
            }
            Err(e) => {
                tracing::warn!("Failed to parse regime label: {e}");
            }
        }
    }
    Ok(())
}
