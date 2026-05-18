use crate::error::{Result, SensoryError};

/// Runtime configuration loaded from environment variables.
#[derive(Debug, Clone)]
pub struct Config {
    /// Polygon.io API key
    pub polygon_api_key: String,

    /// WebSocket endpoint (default: wss://socket.polygon.io/stocks)
    pub polygon_ws_url: String,

    /// Comma-separated list of symbols to subscribe (e.g. "AAPL,MSFT,TSLA")
    /// Use "*" for all tickers (requires appropriate Polygon plan)
    pub symbols: Vec<String>,

    /// QuestDB ILP TCP host
    pub questdb_ilp_host: String,

    /// QuestDB ILP TCP port (default: 9009)
    pub questdb_ilp_port: u16,

    /// Redis connection URL
    pub redis_url: String,

    /// TTL for L2 order book freshness in milliseconds (default: 1)
    pub freshness_l2_ms: u64,

    /// TTL for trade print freshness in milliseconds (default: 5)
    pub freshness_print_ms: u64,

    /// Rolling window size for Z-score / MAD / realized vol (default: 20)
    pub rolling_window: usize,

    /// Rolling window for ADV computation in days (default: 30)
    pub adv_window_days: usize,

    /// Max reconnect attempts before CRITICAL log (default: 5)
    pub max_reconnect_attempts: u32,

    /// Log level (default: info)
    pub log_level: String,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        dotenvy::dotenv().ok(); // load .env if present; ignore error if missing

        let polygon_api_key = require_env("POLYGON_API_KEY")?;
        let symbols_raw = std::env::var("POLYGON_SYMBOLS")
            .unwrap_or_else(|_| "AAPL,MSFT,GOOGL,TSLA,NVDA,AMZN,META,NFLX".to_string());
        let symbols = symbols_raw
            .split(',')
            .map(|s| s.trim().to_uppercase())
            .filter(|s| !s.is_empty())
            .collect();

        Ok(Config {
            polygon_api_key,
            polygon_ws_url: std::env::var("POLYGON_WS_URL")
                .unwrap_or_else(|_| "wss://socket.polygon.io/stocks".to_string()),
            symbols,
            questdb_ilp_host: std::env::var("QUESTDB_ILP_HOST")
                .unwrap_or_else(|_| "localhost".to_string()),
            questdb_ilp_port: parse_env("QUESTDB_ILP_PORT", 9009)?,
            redis_url: std::env::var("REDIS_URL")
                .unwrap_or_else(|_| "redis://localhost:6379".to_string()),
            freshness_l2_ms: parse_env("FRESHNESS_L2_MS", 1)?,
            freshness_print_ms: parse_env("FRESHNESS_PRINT_MS", 5)?,
            rolling_window: parse_env("ROLLING_WINDOW", 20)?,
            adv_window_days: parse_env("ADV_WINDOW_DAYS", 30)?,
            max_reconnect_attempts: parse_env("MAX_RECONNECT_ATTEMPTS", 5)?,
            log_level: std::env::var("LOG_LEVEL").unwrap_or_else(|_| "info".to_string()),
        })
    }
}

fn require_env(key: &str) -> Result<String> {
    std::env::var(key).map_err(|_| SensoryError::Config {
        msg: format!("Required env var '{key}' is not set"),
    })
}

fn parse_env<T: std::str::FromStr>(key: &str, default: T) -> Result<T>
where
    T::Err: std::fmt::Display,
{
    match std::env::var(key) {
        Ok(val) => val.parse::<T>().map_err(|e| SensoryError::Config {
            msg: format!("Failed to parse '{key}={val}': {e}"),
        }),
        Err(_) => Ok(default),
    }
}
