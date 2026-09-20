//! Environment configuration, validated at startup (fail fast, fail closed).

use std::fmt;
use std::time::Duration;

use crate::error::{Result, SensoryError};
use crate::normalizer::{MAX_WINDOW, MIN_WINDOW};
use crate::validate::{is_valid_ticker, MAX_SYMBOLS};

const DEFAULT_SYMBOLS: &str = "AAPL,MSFT,GOOGL,TSLA,NVDA,AMZN,META,NFLX";

/// Runtime configuration. `Debug` is hand-written so secrets never reach logs.
#[derive(Clone)]
pub struct Config {
    /// Polygon.io API key (secret).
    pub polygon_api_key: String,
    /// WebSocket endpoint; must be `wss://` (validated).
    pub polygon_ws_url: String,
    /// Symbols to subscribe to, or `["*"]` for every ticker.
    pub symbols: Vec<String>,

    pub questdb_ilp_host: String,
    pub questdb_ilp_port: u16,
    /// Wrap the ILP connection in TLS (requires the `ilp-secure` feature).
    pub questdb_tls: bool,
    /// ILP auth key id (`kid` of the QuestDB auth.conf entry).
    pub questdb_auth_key_id: Option<String>,
    /// ILP auth private key: base64url `d` component of the P-256 JWK (secret).
    pub questdb_auth_token: Option<String>,
    /// Extra PEM CA bundle to trust for the QuestDB TLS endpoint.
    pub questdb_tls_ca_file: Option<String>,
    pub questdb_connect_timeout_ms: u64,
    pub questdb_write_timeout_ms: u64,
    pub questdb_flush_interval_ms: u64,
    pub questdb_queue_capacity: usize,

    pub redis_url: String,
    pub redis_queue_capacity: usize,

    /// Max age of an L2 quote inside our pipeline (ms). Spec: 1.
    pub freshness_l2_ms: u64,
    /// Max age of a trade print inside our pipeline (ms). Spec: 5.
    pub freshness_print_ms: u64,
    /// Max |now - SIP timestamp| lag before a quote is flagged (ms).
    pub feed_max_lag_ms: u64,
    /// How far an exchange timestamp may lead the local clock (ms).
    pub feed_future_tolerance_ms: u64,

    pub rolling_window: usize,
    pub adv_window_days: usize,

    /// After this many consecutive failed sessions a CRITICAL event is logged
    /// on every further attempt. Reconnecting never stops.
    pub max_reconnect_attempts: u32,
    pub reconnect_base_ms: u64,
    pub reconnect_max_ms: u64,
    /// A session that lived at least this long resets the failure counter.
    pub reconnect_stable_ms: u64,
    pub ws_connect_timeout_ms: u64,
    pub ws_handshake_timeout_ms: u64,
    /// No frame for this long triggers a ping; no reply for another period
    /// tears the connection down.
    pub ws_idle_timeout_ms: u64,

    /// Regime labels older than this are served as `REGIME_UNKNOWN` (seconds).
    pub regime_max_age_s: u64,

    /// Heartbeat file used by the container HEALTHCHECK.
    pub health_file: String,
    pub log_level: String,
}

impl Default for Config {
    /// Spec defaults. `polygon_api_key` is empty, so a default config is
    /// deliberately NOT valid until a key is supplied.
    fn default() -> Self {
        Config {
            polygon_api_key: String::new(),
            polygon_ws_url: "wss://socket.polygon.io/stocks".to_string(),
            symbols: DEFAULT_SYMBOLS.split(',').map(str::to_string).collect(),
            questdb_ilp_host: "localhost".to_string(),
            questdb_ilp_port: 9009,
            questdb_tls: false,
            questdb_auth_key_id: None,
            questdb_auth_token: None,
            questdb_tls_ca_file: None,
            questdb_connect_timeout_ms: 2_000,
            questdb_write_timeout_ms: 2_000,
            questdb_flush_interval_ms: 10,
            questdb_queue_capacity: 65_536,
            redis_url: "redis://localhost:6379".to_string(),
            redis_queue_capacity: 8_192,
            freshness_l2_ms: 1,
            freshness_print_ms: 5,
            feed_max_lag_ms: 1_000,
            feed_future_tolerance_ms: 1_000,
            rolling_window: 20,
            adv_window_days: 30,
            max_reconnect_attempts: 5,
            reconnect_base_ms: 1_000,
            reconnect_max_ms: 16_000,
            reconnect_stable_ms: 5_000,
            ws_connect_timeout_ms: 10_000,
            ws_handshake_timeout_ms: 10_000,
            ws_idle_timeout_ms: 30_000,
            regime_max_age_s: 15,
            health_file: "/tmp/sensory-array.health".to_string(),
            log_level: "info".to_string(),
        }
    }
}

impl fmt::Debug for Config {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Config")
            .field("polygon_api_key", &"<redacted>")
            .field("polygon_ws_url", &self.polygon_ws_url)
            .field("symbols", &self.symbols)
            .field("questdb_ilp_host", &self.questdb_ilp_host)
            .field("questdb_ilp_port", &self.questdb_ilp_port)
            .field("questdb_tls", &self.questdb_tls)
            .field("questdb_auth_key_id", &self.questdb_auth_key_id)
            .field(
                "questdb_auth_token",
                &self.questdb_auth_token.as_ref().map(|_| "<redacted>"),
            )
            .field("questdb_tls_ca_file", &self.questdb_tls_ca_file)
            .field("redis_url", &redact_url(&self.redis_url))
            .field("freshness_l2_ms", &self.freshness_l2_ms)
            .field("freshness_print_ms", &self.freshness_print_ms)
            .field("feed_max_lag_ms", &self.feed_max_lag_ms)
            .field("rolling_window", &self.rolling_window)
            .field("adv_window_days", &self.adv_window_days)
            .field("max_reconnect_attempts", &self.max_reconnect_attempts)
            .field("regime_max_age_s", &self.regime_max_age_s)
            .field("log_level", &self.log_level)
            .finish_non_exhaustive()
    }
}

/// Replace any `user:password@` section of a URL with `<redacted>@`.
pub fn redact_url(url: &str) -> String {
    match (url.find("://"), url.rfind('@')) {
        (Some(scheme_end), Some(at)) if at > scheme_end + 3 => {
            format!("{}<redacted>{}", &url[..scheme_end + 3], &url[at..])
        }
        _ => url.to_string(),
    }
}

impl Config {
    /// Load from the process environment (after reading `.env` if present).
    pub fn from_env() -> Result<Self> {
        dotenvy::dotenv().ok(); // a missing .env is fine
        Self::from_lookup(|k| std::env::var(k).ok())
    }

    /// Load through an arbitrary lookup (used by tests; no global state).
    pub fn from_lookup<F: Fn(&str) -> Option<String>>(get: F) -> Result<Self> {
        let d = Config::default();
        let symbols = match get("POLYGON_SYMBOLS") {
            Some(raw) => raw
                .split(',')
                .map(|s| s.trim().to_uppercase())
                .filter(|s| !s.is_empty())
                .collect(),
            None => d.symbols.clone(),
        };
        let cfg = Config {
            polygon_api_key: get("POLYGON_API_KEY").ok_or_else(|| missing("POLYGON_API_KEY"))?,
            polygon_ws_url: get("POLYGON_WS_URL").unwrap_or(d.polygon_ws_url),
            symbols,
            questdb_ilp_host: get("QUESTDB_ILP_HOST").unwrap_or(d.questdb_ilp_host),
            questdb_ilp_port: parse(&get, "QUESTDB_ILP_PORT", d.questdb_ilp_port)?,
            questdb_tls: parse_bool(&get, "QUESTDB_ILP_TLS", d.questdb_tls)?,
            questdb_auth_key_id: get("QUESTDB_ILP_AUTH_KEY_ID").filter(|s| !s.is_empty()),
            questdb_auth_token: get("QUESTDB_ILP_AUTH_TOKEN").filter(|s| !s.is_empty()),
            questdb_tls_ca_file: get("QUESTDB_ILP_TLS_CA_FILE").filter(|s| !s.is_empty()),
            questdb_connect_timeout_ms: parse(
                &get,
                "QUESTDB_CONNECT_TIMEOUT_MS",
                d.questdb_connect_timeout_ms,
            )?,
            questdb_write_timeout_ms: parse(
                &get,
                "QUESTDB_WRITE_TIMEOUT_MS",
                d.questdb_write_timeout_ms,
            )?,
            questdb_flush_interval_ms: parse(
                &get,
                "QUESTDB_FLUSH_INTERVAL_MS",
                d.questdb_flush_interval_ms,
            )?,
            questdb_queue_capacity: parse(
                &get,
                "QUESTDB_QUEUE_CAPACITY",
                d.questdb_queue_capacity,
            )?,
            redis_url: resolve_redis_url(&get)?.unwrap_or(d.redis_url),
            redis_queue_capacity: parse(&get, "REDIS_QUEUE_CAPACITY", d.redis_queue_capacity)?,
            freshness_l2_ms: parse(&get, "FRESHNESS_L2_MS", d.freshness_l2_ms)?,
            freshness_print_ms: parse(&get, "FRESHNESS_PRINT_MS", d.freshness_print_ms)?,
            feed_max_lag_ms: parse(&get, "FEED_MAX_LAG_MS", d.feed_max_lag_ms)?,
            feed_future_tolerance_ms: parse(
                &get,
                "FEED_FUTURE_TOLERANCE_MS",
                d.feed_future_tolerance_ms,
            )?,
            rolling_window: parse(&get, "ROLLING_WINDOW", d.rolling_window)?,
            adv_window_days: parse(&get, "ADV_WINDOW_DAYS", d.adv_window_days)?,
            max_reconnect_attempts: parse(
                &get,
                "MAX_RECONNECT_ATTEMPTS",
                d.max_reconnect_attempts,
            )?,
            reconnect_base_ms: parse(&get, "RECONNECT_BASE_MS", d.reconnect_base_ms)?,
            reconnect_max_ms: parse(&get, "RECONNECT_MAX_MS", d.reconnect_max_ms)?,
            reconnect_stable_ms: parse(&get, "RECONNECT_STABLE_MS", d.reconnect_stable_ms)?,
            ws_connect_timeout_ms: parse(&get, "WS_CONNECT_TIMEOUT_MS", d.ws_connect_timeout_ms)?,
            ws_handshake_timeout_ms: parse(
                &get,
                "WS_HANDSHAKE_TIMEOUT_MS",
                d.ws_handshake_timeout_ms,
            )?,
            ws_idle_timeout_ms: parse(&get, "WS_IDLE_TIMEOUT_MS", d.ws_idle_timeout_ms)?,
            regime_max_age_s: parse(&get, "REGIME_MAX_AGE_S", d.regime_max_age_s)?,
            health_file: get("HEALTH_FILE").unwrap_or(d.health_file),
            log_level: get("LOG_LEVEL").unwrap_or(d.log_level),
        };
        cfg.validate()?;
        Ok(cfg)
    }

    /// Range and consistency checks. Called by `from_lookup`; also usable on
    /// hand-built configs.
    pub fn validate(&self) -> Result<()> {
        self.validate_secrets_and_urls()?;
        self.validate_symbols()?;
        self.validate_numeric_ranges()?;
        self.validate_questdb_security()
    }

    fn validate_secrets_and_urls(&self) -> Result<()> {
        let key = &self.polygon_api_key;
        if key.is_empty() || key.len() > 128 || key.chars().any(|c| c.is_whitespace() || c.is_control()) {
            return bad("POLYGON_API_KEY must be 1-128 chars without whitespace/control chars");
        }
        if !self.polygon_ws_url.starts_with("wss://") || self.polygon_ws_url.len() <= "wss://".len()
        {
            return bad("POLYGON_WS_URL must be a wss:// URL (plaintext ws:// would leak the API key)");
        }
        if !(self.redis_url.starts_with("redis://") || self.redis_url.starts_with("rediss://")) {
            return bad("REDIS_URL must start with redis:// or rediss://");
        }
        if self.questdb_ilp_host.trim().is_empty() {
            return bad("QUESTDB_ILP_HOST must not be empty");
        }
        Ok(())
    }

    fn validate_symbols(&self) -> Result<()> {
        if self.symbols.is_empty() {
            return bad("POLYGON_SYMBOLS must contain at least one symbol");
        }
        let has_wildcard = self.symbols.iter().any(|s| s == "*");
        if has_wildcard && self.symbols.len() != 1 {
            return bad("POLYGON_SYMBOLS: '*' cannot be combined with other symbols");
        }
        if self.symbols.len() > MAX_SYMBOLS {
            return bad("POLYGON_SYMBOLS: too many symbols");
        }
        if let Some(s) = self.symbols.iter().find(|s| *s != "*" && !is_valid_ticker(s)) {
            return bad(&format!("POLYGON_SYMBOLS: invalid ticker '{s}'"));
        }
        Ok(())
    }

    fn validate_numeric_ranges(&self) -> Result<()> {
        check_range("ROLLING_WINDOW", self.rolling_window as u64, MIN_WINDOW as u64, MAX_WINDOW as u64)?;
        check_range("ADV_WINDOW_DAYS", self.adv_window_days as u64, 1, 365)?;
        check_range("QUESTDB_ILP_PORT", u64::from(self.questdb_ilp_port), 1, 65_535)?;
        check_range("FRESHNESS_L2_MS", self.freshness_l2_ms, 1, 60_000)?;
        check_range("FRESHNESS_PRINT_MS", self.freshness_print_ms, 1, 60_000)?;
        check_range("FEED_MAX_LAG_MS", self.feed_max_lag_ms, 1, 3_600_000)?;
        check_range("FEED_FUTURE_TOLERANCE_MS", self.feed_future_tolerance_ms, 0, 60_000)?;
        check_range("MAX_RECONNECT_ATTEMPTS", u64::from(self.max_reconnect_attempts), 1, 1_000)?;
        check_range("RECONNECT_BASE_MS", self.reconnect_base_ms, 1, 60_000)?;
        check_range("RECONNECT_MAX_MS", self.reconnect_max_ms, self.reconnect_base_ms, 600_000)?;
        check_range("WS_CONNECT_TIMEOUT_MS", self.ws_connect_timeout_ms, 1, 120_000)?;
        check_range("WS_HANDSHAKE_TIMEOUT_MS", self.ws_handshake_timeout_ms, 1, 120_000)?;
        check_range("WS_IDLE_TIMEOUT_MS", self.ws_idle_timeout_ms, 1, 600_000)?;
        check_range("QUESTDB_CONNECT_TIMEOUT_MS", self.questdb_connect_timeout_ms, 1, 60_000)?;
        check_range("QUESTDB_WRITE_TIMEOUT_MS", self.questdb_write_timeout_ms, 1, 60_000)?;
        check_range("QUESTDB_FLUSH_INTERVAL_MS", self.questdb_flush_interval_ms, 1, 5_000)?;
        check_range("QUESTDB_QUEUE_CAPACITY", self.questdb_queue_capacity as u64, 16, 10_000_000)?;
        check_range("REDIS_QUEUE_CAPACITY", self.redis_queue_capacity as u64, 16, 10_000_000)?;
        check_range("REGIME_MAX_AGE_S", self.regime_max_age_s, 1, 3_600)
    }

    fn validate_questdb_security(&self) -> Result<()> {
        let wants_secure = self.questdb_tls || self.questdb_auth_key_id.is_some();
        if wants_secure && !cfg!(feature = "ilp-secure") {
            return bad(
                "QUESTDB_ILP_TLS / QUESTDB_ILP_AUTH_* requested but this binary was built \
                 without the `ilp-secure` cargo feature",
            );
        }
        if self.questdb_auth_key_id.is_some() != self.questdb_auth_token.is_some() {
            return bad("QUESTDB_ILP_AUTH_KEY_ID and QUESTDB_ILP_AUTH_TOKEN must be set together");
        }
        if self.questdb_tls_ca_file.is_some() && !self.questdb_tls {
            return bad("QUESTDB_ILP_TLS_CA_FILE requires QUESTDB_ILP_TLS=true");
        }
        Ok(())
    }

    // Convenience accessors ------------------------------------------------

    pub fn ws_connect_timeout(&self) -> Duration {
        Duration::from_millis(self.ws_connect_timeout_ms)
    }
    pub fn ws_handshake_timeout(&self) -> Duration {
        Duration::from_millis(self.ws_handshake_timeout_ms)
    }
    pub fn ws_idle_timeout(&self) -> Duration {
        Duration::from_millis(self.ws_idle_timeout_ms)
    }
    pub fn reconnect_base(&self) -> Duration {
        Duration::from_millis(self.reconnect_base_ms)
    }
    pub fn reconnect_max(&self) -> Duration {
        Duration::from_millis(self.reconnect_max_ms)
    }
    pub fn reconnect_stable(&self) -> Duration {
        Duration::from_millis(self.reconnect_stable_ms)
    }
}

fn bad(msg: &str) -> Result<()> {
    Err(SensoryError::Config {
        msg: msg.to_string(),
    })
}

fn missing(key: &str) -> SensoryError {
    SensoryError::Config {
        msg: format!("Required env var '{key}' is not set"),
    }
}

fn check_range(name: &str, value: u64, min: u64, max: u64) -> Result<()> {
    if (min..=max).contains(&value) {
        Ok(())
    } else {
        bad(&format!("{name}={value} out of range [{min}, {max}]"))
    }
}

fn parse<T, F>(get: &F, key: &str, default: T) -> Result<T>
where
    T: std::str::FromStr,
    T::Err: fmt::Display,
    F: Fn(&str) -> Option<String>,
{
    match get(key) {
        Some(val) => val.trim().parse::<T>().map_err(|e| SensoryError::Config {
            msg: format!("Failed to parse '{key}': {e}"),
        }),
        None => Ok(default),
    }
}

fn parse_bool<F: Fn(&str) -> Option<String>>(get: &F, key: &str, default: bool) -> Result<bool> {
    match get(key).map(|v| v.trim().to_ascii_lowercase()) {
        None => Ok(default),
        Some(v) if matches!(v.as_str(), "1" | "true" | "yes" | "on") => Ok(true),
        Some(v) if matches!(v.as_str(), "0" | "false" | "no" | "off") => Ok(false),
        Some(_) => Err(SensoryError::Config {
            msg: format!("'{key}' must be a boolean (true/false)"),
        }),
    }
}

/// `REDIS_URL`, else `redis://REDIS_HOST:REDIS_PORT` (the compose file passes
/// host/port separately).
fn resolve_redis_url<F: Fn(&str) -> Option<String>>(get: &F) -> Result<Option<String>> {
    if let Some(url) = get("REDIS_URL") {
        return Ok(Some(url));
    }
    let Some(host) = get("REDIS_HOST").filter(|h| !h.trim().is_empty()) else {
        return Ok(None);
    };
    let port: u16 = parse(get, "REDIS_PORT", 6379)?;
    Ok(Some(format!("redis://{}:{port}", host.trim())))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        move |k| map.get(k).cloned()
    }

    fn base() -> Vec<(&'static str, &'static str)> {
        vec![("POLYGON_API_KEY", "test-key-123")]
    }

    fn with(extra: &[(&'static str, &'static str)]) -> Result<Config> {
        let mut v = base();
        v.extend_from_slice(extra);
        Config::from_lookup(lookup(&v))
    }

    #[test]
    fn defaults_are_spec_values() {
        let c = with(&[]).expect("valid");
        assert_eq!(c.freshness_l2_ms, 1);
        assert_eq!(c.freshness_print_ms, 5);
        assert_eq!(c.rolling_window, 20);
        assert_eq!(c.adv_window_days, 30);
        assert_eq!(c.polygon_ws_url, "wss://socket.polygon.io/stocks");
    }

    #[test]
    fn missing_api_key_is_an_error() {
        assert!(Config::from_lookup(lookup(&[])).is_err());
    }

    #[test]
    fn rejects_plaintext_ws_url() {
        assert!(with(&[("POLYGON_WS_URL", "ws://socket.polygon.io/stocks")]).is_err());
        assert!(with(&[("POLYGON_WS_URL", "http://x")]).is_err());
        assert!(with(&[("POLYGON_WS_URL", "wss://")]).is_err());
    }

    #[test]
    fn rejects_out_of_range_values() {
        for (k, v) in [
            ("ROLLING_WINDOW", "0"),
            ("ROLLING_WINDOW", "1"),
            ("ROLLING_WINDOW", "100000"),
            ("ADV_WINDOW_DAYS", "0"),
            ("FRESHNESS_L2_MS", "0"),
            ("QUESTDB_ILP_PORT", "0"),
            ("MAX_RECONNECT_ATTEMPTS", "0"),
            ("RECONNECT_BASE_MS", "0"),
            ("REGIME_MAX_AGE_S", "0"),
            ("WS_IDLE_TIMEOUT_MS", "0"),
            ("QUESTDB_QUEUE_CAPACITY", "1"),
        ] {
            assert!(with(&[(k, v)]).is_err(), "{k}={v} must be rejected");
        }
    }

    #[test]
    fn rejects_unparsable_numbers() {
        assert!(with(&[("ROLLING_WINDOW", "abc")]).is_err());
        assert!(with(&[("QUESTDB_ILP_PORT", "70000")]).is_err());
    }

    #[test]
    fn reconnect_max_must_not_be_below_base() {
        assert!(with(&[("RECONNECT_BASE_MS", "5000"), ("RECONNECT_MAX_MS", "1000")]).is_err());
    }

    #[test]
    fn symbol_list_is_validated() {
        assert!(with(&[("POLYGON_SYMBOLS", "aapl, msft")]).is_ok());
        assert!(with(&[("POLYGON_SYMBOLS", "AAPL,BAD SYMBOL")]).is_err());
        assert!(with(&[("POLYGON_SYMBOLS", "*")]).is_ok());
        assert!(with(&[("POLYGON_SYMBOLS", "*,AAPL")]).is_err());
        assert!(with(&[("POLYGON_SYMBOLS", " , ")]).is_err());
    }

    #[test]
    fn api_key_shape_is_validated() {
        let get = lookup(&[("POLYGON_API_KEY", "has space")]);
        assert!(Config::from_lookup(get).is_err());
        let get = lookup(&[("POLYGON_API_KEY", "")]);
        assert!(Config::from_lookup(get).is_err());
    }

    #[test]
    fn debug_output_redacts_secrets() {
        let mut c = with(&[("REDIS_URL", "redis://user:hunter2@redis:6379")]).expect("valid");
        c.questdb_auth_token = Some("super-secret-token".into());
        let dbg = format!("{c:?}");
        assert!(!dbg.contains("test-key-123"), "{dbg}");
        assert!(!dbg.contains("hunter2"), "{dbg}");
        assert!(!dbg.contains("super-secret-token"), "{dbg}");
        assert!(dbg.contains("<redacted>"));
    }

    #[test]
    fn redis_url_falls_back_to_host_and_port() {
        let c = with(&[("REDIS_HOST", "redis"), ("REDIS_PORT", "6380")]).expect("valid");
        assert_eq!(c.redis_url, "redis://redis:6380");
        let c = with(&[("REDIS_HOST", "redis")]).expect("valid");
        assert_eq!(c.redis_url, "redis://redis:6379");
        let c = with(&[("REDIS_URL", "redis://a:1"), ("REDIS_HOST", "b")]).expect("valid");
        assert_eq!(c.redis_url, "redis://a:1");
    }

    #[test]
    fn redact_url_handles_shapes() {
        assert_eq!(redact_url("redis://redis:6379"), "redis://redis:6379");
        assert_eq!(redact_url("redis://:pw@redis:6379"), "redis://<redacted>@redis:6379");
    }

    #[cfg(not(feature = "ilp-secure"))]
    #[test]
    fn secure_ilp_requires_the_cargo_feature() {
        assert!(with(&[("QUESTDB_ILP_TLS", "true")]).is_err());
    }

    #[test]
    fn auth_key_and_token_must_pair() {
        // Fails either way: unpaired (feature on) or feature missing (feature off).
        assert!(with(&[("QUESTDB_ILP_AUTH_KEY_ID", "kid")]).is_err());
        assert!(with(&[("QUESTDB_ILP_TLS_CA_FILE", "/ca.pem")]).is_err());
    }

    #[test]
    fn bool_parsing() {
        assert!(with(&[("QUESTDB_ILP_TLS", "maybe")]).is_err());
    }
}
