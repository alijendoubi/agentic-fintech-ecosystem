use std::net::{SocketAddr, TcpStream};
use std::process::ExitCode;
use std::time::Duration;

use aegis::config::RuntimeConfig;

const HEALTHCHECK_TIMEOUT: Duration = Duration::from_secs(2);

fn init_tracing() {
    let filter = tracing_subscriber::EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info"));
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(filter)
        .init();
}

/// `aegis healthcheck`: succeeds iff something accepts TCP connections on the
/// configured listen port (loopback). Needs no shell tools, so it works in a
/// minimal image. It does not authenticate (it would need a client cert).
fn healthcheck() -> ExitCode {
    let listen = std::env::var("AEGIS_LISTEN_ADDR").unwrap_or_else(|_| "0.0.0.0:50051".to_owned());
    let Ok(addr) = listen.parse::<SocketAddr>() else {
        return ExitCode::from(2);
    };
    let target = SocketAddr::from(([127, 0, 0, 1], addr.port()));
    match TcpStream::connect_timeout(&target, HEALTHCHECK_TIMEOUT) {
        Ok(_) => ExitCode::SUCCESS,
        Err(_) => ExitCode::FAILURE,
    }
}

/// `aegis supervisor`: the independent liveness Supervisor (its own process).
/// Any failure, including bad configuration, is a non-zero exit.
fn supervisor() -> ExitCode {
    init_tracing();
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "supervisor cannot start the async runtime");
            return ExitCode::from(1);
        }
    };
    match runtime.block_on(aegis::supervisor::run_from_env()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %e, "supervisor stopped: failing closed");
            ExitCode::from(e.exit_code())
        }
    }
}

/// `aegis supervisor-healthcheck`: succeeds iff the Supervisor's heartbeat file
/// is fresh (ALI-163), so a hung Supervisor turns its container unhealthy.
fn supervisor_healthcheck() -> ExitCode {
    ExitCode::from(aegis::supervisor::heartbeat::healthcheck_exit_code(&|k| {
        std::env::var(k).ok()
    }))
}

fn main() -> ExitCode {
    match std::env::args().nth(1).as_deref() {
        Some("healthcheck") => return healthcheck(),
        Some("supervisor") => return supervisor(),
        Some("supervisor-healthcheck") => return supervisor_healthcheck(),
        Some(other) => {
            eprintln!(
                "unknown command {other:?}; use no argument, `supervisor`, `healthcheck` or `supervisor-healthcheck`"
            );
            return ExitCode::from(2);
        }
        None => {}
    }
    init_tracing();
    let cfg = match RuntimeConfig::from_env() {
        Ok(c) => c,
        Err(e) => {
            tracing::error!(error = %e, "refusing to start: invalid runtime configuration");
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "cannot start the async runtime");
            return ExitCode::from(1);
        }
    };
    match runtime.block_on(aegis::app::run(cfg)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %e, "refusing to start or terminated");
            ExitCode::from(2)
        }
    }
}
