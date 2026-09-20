use std::process::ExitCode;

use aegis::config::RuntimeConfig;
use aegis::limits::Limits;

fn init_tracing() {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(filter)
        .init();
}

fn main() -> ExitCode {
    init_tracing();
    let runtime = match RuntimeConfig::from_env() {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "refusing to start: invalid runtime configuration");
            return ExitCode::from(2);
        }
    };
    let limits = match Limits::from_path(&runtime.limits_file) {
        Ok(l) => l,
        Err(e) => {
            tracing::error!(error = %e, "refusing to start: risk limits not configured");
            return ExitCode::from(2);
        }
    };
    tracing::info!(limits_sha256 = %limits.sha256_hex, "configuration loaded");
    ExitCode::SUCCESS
}
