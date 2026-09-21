//! Transport: mTLS (rustls) or, only for local development, plaintext.
//!
//! Server certificate + key and the CLIENT CA come from files. The client CA
//! is mandatory and client certificates are REQUIRED (no optional client
//! auth): a connection without a certificate chaining to that CA never reaches
//! a handler.

use std::future::Future;
use std::sync::Arc;
use std::time::Duration;

use thiserror::Error;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;
use tonic::transport::{Certificate, Identity, Server, ServerTlsConfig};

use crate::config::TlsPaths;
use crate::engine::Engine;
use crate::pb::AegisServer;
use crate::service::AegisService;

/// Interval of the background housekeeping tick (dead-man's switch, expiry).
const TICK: Duration = Duration::from_secs(1);
/// HTTP/2 keep-alive so dead peers do not hold permits or streams.
const KEEPALIVE: Duration = Duration::from_secs(30);
const PER_CONNECTION_STREAMS: usize = 32;

#[derive(Debug, Error)]
pub enum ServeError {
    #[error("cannot read TLS material {what}: {detail}")]
    TlsFile { what: &'static str, detail: String },
    #[error("transport error: {0}")]
    Transport(#[from] tonic::transport::Error),
    #[error("cannot listen: {0}")]
    Listen(#[from] std::io::Error),
}

fn read(path: &std::path::Path, what: &'static str) -> Result<Vec<u8>, ServeError> {
    std::fs::read(path).map_err(|e| ServeError::TlsFile {
        what,
        detail: e.to_string(),
    })
}

/// Build the mutual-TLS server configuration from PEM files.
pub fn load_tls(paths: &TlsPaths) -> Result<ServerTlsConfig, ServeError> {
    let cert = read(&paths.cert, "server certificate")?;
    let key = read(&paths.key, "server key")?;
    let ca = read(&paths.client_ca, "client CA")?;
    Ok(ServerTlsConfig::new()
        .identity(Identity::from_pem(cert, key))
        .client_ca_root(Certificate::from_pem(ca)))
}

/// Serve on an already-bound listener. `tls = None` is plaintext (dev only).
pub async fn serve_on<F>(
    listener: TcpListener,
    tls: Option<ServerTlsConfig>,
    service: AegisService,
    rpc_cap: Duration,
    shutdown: F,
) -> Result<(), ServeError>
where
    F: Future<Output = ()>,
{
    let mut builder = Server::builder()
        .timeout(rpc_cap)
        .concurrency_limit_per_connection(PER_CONNECTION_STREAMS)
        .http2_keepalive_interval(Some(KEEPALIVE))
        .http2_keepalive_timeout(Some(KEEPALIVE));
    if let Some(cfg) = tls {
        builder = builder.tls_config(cfg)?;
    } else {
        tracing::warn!("serving WITHOUT TLS (AEGIS_INSECURE_DEV=1): development only");
    }
    builder
        .add_service(AegisServer::new(service))
        .serve_with_incoming_shutdown(TcpListenerStream::new(listener), shutdown)
        .await?;
    Ok(())
}

/// Background housekeeping: kill-switch tick and reservation/hold expiry.
pub fn spawn_ticker(engine: Arc<Engine>) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(TICK);
        loop {
            interval.tick().await;
            let e = engine.clone();
            let _ = tokio::task::spawn_blocking(move || {
                e.deps.kill.tick();
                e.maintenance();
            })
            .await;
        }
    })
}

/// Resolves on SIGINT or SIGTERM.
pub async fn shutdown_signal() {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        match signal(SignalKind::terminate()) {
            Ok(mut term) => {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = term.recv() => {}
                }
            }
            Err(_) => {
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = tokio::signal::ctrl_c().await;
    }
}
