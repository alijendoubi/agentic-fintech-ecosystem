use thiserror::Error;

/// All failure modes of the sensory array.
///
/// Every variant is either handled explicitly (reconnect / drop-and-count) or
/// bubbles up to `main`, which exits non-zero (fail closed).
#[derive(Debug, Error)]
pub enum SensoryError {
    #[error("WebSocket error: {0}")]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),

    #[error("JSON parse error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("Redis error: {0}")]
    Redis(#[from] redis::RedisError),

    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    #[error("Config error: {msg}")]
    Config { msg: String },

    /// Bad credentials. Retrying cannot fix this, so it is fatal.
    #[error("Authentication failed: {reason}")]
    AuthFailed { reason: String },

    /// Recoverable protocol / liveness failure (handshake timeout, idle
    /// watchdog, server-side connection limit, ...). The caller reconnects.
    #[error("Feed session failed: {reason}")]
    Session { reason: String },
}

impl SensoryError {
    /// Whether the failure can never be fixed by reconnecting.
    pub fn is_fatal(&self) -> bool {
        matches!(
            self,
            SensoryError::AuthFailed { .. } | SensoryError::Config { .. }
        )
    }

    pub fn session(reason: impl Into<String>) -> Self {
        SensoryError::Session {
            reason: reason.into(),
        }
    }
}

pub type Result<T> = std::result::Result<T, SensoryError>;
