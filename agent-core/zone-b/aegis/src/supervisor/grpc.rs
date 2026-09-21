//! gRPC implementation of [`AegisLink`]: probes with `GetAegisState` (which
//! runs on Aegis's engine worker pool, so a wedged engine fails the probe, not
//! just a live socket) and trips with `TriggerKillSwitch`.
//!
//! The channel is lazy and reconnects by itself; a connect or handshake failure
//! is simply a failed probe. TLS material is read once at start-up: unreadable
//! files, or a certificate/target the transport rejects, are set-up errors
//! (non-zero exit), not probe failures.

use std::time::Duration;

use tonic::transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity};

use super::config::{SupervisorConfig, SupervisorTls};
use super::{AegisLink, LinkError, SupervisorError};
use crate::killswitch::watchdog::SUPERVISOR_ACTOR;
use crate::pb::aegis_client::AegisClient;
use crate::pb::{Empty, KillSwitchLevel, TriggerKillSwitchRequest};

const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);

pub struct GrpcAegis {
    client: AegisClient<Channel>,
    timeout: Duration,
}

fn setup(what: &str, e: impl std::fmt::Display) -> SupervisorError {
    SupervisorError::Setup(format!("{what}: {e}"))
}

fn read(path: &std::path::Path, what: &str) -> Result<Vec<u8>, SupervisorError> {
    std::fs::read(path).map_err(|e| setup(what, e))
}

fn host_of(target: &str) -> &str {
    let rest = target.split_once("://").map_or(target, |(_, r)| r);
    let authority = rest.split('/').next().unwrap_or(rest);
    authority.rsplit_once(':').map_or(authority, |(h, _)| h)
}

impl GrpcAegis {
    /// Build the lazy channel. Must run inside a tokio runtime.
    pub fn connect(cfg: &SupervisorConfig) -> Result<GrpcAegis, SupervisorError> {
        let mut endpoint = Endpoint::from_shared(cfg.target.clone())
            .map_err(|e| setup("invalid target", e))?
            .connect_timeout(CONNECT_TIMEOUT)
            .timeout(cfg.probe_timeout);
        if let SupervisorTls::Mutual(paths) = &cfg.tls {
            let ca = read(&paths.ca, "server CA")?;
            let cert = read(&paths.cert, "client certificate")?;
            let key = read(&paths.key, "client key")?;
            let domain = paths
                .domain
                .clone()
                .unwrap_or_else(|| host_of(&cfg.target).to_owned());
            let tls = ClientTlsConfig::new()
                .ca_certificate(Certificate::from_pem(ca))
                .identity(Identity::from_pem(cert, key))
                .domain_name(domain);
            endpoint = endpoint
                .tls_config(tls)
                .map_err(|e| setup("tls configuration", e))?;
        }
        Ok(GrpcAegis {
            client: AegisClient::new(endpoint.connect_lazy()),
            timeout: cfg.probe_timeout,
        })
    }
}

fn status(e: tonic::Status) -> LinkError {
    LinkError(format!("{}: {}", e.code(), e.message()))
}

#[tonic::async_trait]
impl AegisLink for GrpcAegis {
    async fn probe(&self) -> Result<(), LinkError> {
        let mut client = self.client.clone();
        let call = client.get_aegis_state(Empty {});
        let resp = tokio::time::timeout(self.timeout, call)
            .await
            .map_err(|_| LinkError("probe timed out".into()))?
            .map_err(status)?
            .into_inner();
        match resp.kill {
            Some(_) => Ok(()),
            None => Err(LinkError(
                "aegis answered without a kill-switch state".into(),
            )),
        }
    }

    async fn trip_hard(&self, reason: &str, evidence_ref: &str) -> Result<(), LinkError> {
        let mut client = self.client.clone();
        let call = client.trigger_kill_switch(TriggerKillSwitchRequest {
            level: KillSwitchLevel::KillLevelHard as i32,
            reason: reason.to_owned(),
            actor_id: SUPERVISOR_ACTOR.to_owned(),
            evidence_ref: evidence_ref.to_owned(),
        });
        let state = tokio::time::timeout(self.timeout, call)
            .await
            .map_err(|_| LinkError("trip timed out".into()))?
            .map_err(status)?
            .into_inner();
        if state.effective_level >= KillSwitchLevel::KillLevelHard as i32 {
            Ok(())
        } else {
            Err(LinkError(format!(
                "aegis answered but the effective level is {}, not HARD",
                state.effective_level
            )))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_is_extracted_from_the_target() {
        assert_eq!(host_of("https://aegis:50051"), "aegis");
        assert_eq!(host_of("https://aegis.internal:50051/x"), "aegis.internal");
        assert_eq!(host_of("http://localhost"), "localhost");
    }
}
