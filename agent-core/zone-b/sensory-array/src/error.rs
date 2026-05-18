use thiserror::Error;

#[derive(Debug, Error)]
pub enum SensoryError {
    #[error("WebSocket error: {0}")]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),

    #[error("JSON parse error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("Redis error: {0}")]
    Redis(#[from] redis::RedisError),

    #[error("QuestDB ILP write error: {0}")]
    QuestDb(String),

    #[error("Config error: {msg}")]
    Config { msg: String },

    #[error("Authentication failed: {reason}")]
    AuthFailed { reason: String },

    #[error("Max reconnect attempts ({attempts}) exceeded")]
    MaxReconnectExceeded { attempts: u32 },

    #[error(transparent)]
    Other(#[from] anyhow::Error),
}

pub type Result<T> = std::result::Result<T, SensoryError>;
