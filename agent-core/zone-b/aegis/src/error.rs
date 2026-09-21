//! Startup / configuration errors. Request-path errors live next to the code
//! that raises them and are always mapped to a REJECT decision, never a panic.

use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("required setting {0} is not set")]
    Missing(&'static str),
    #[error("invalid configuration: {0}")]
    Invalid(String),
    #[error("cannot read {what}: {detail}")]
    Unreadable { what: &'static str, detail: String },
}
