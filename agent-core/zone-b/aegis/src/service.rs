//! gRPC `Aegis` service (all 9 RPCs of `aegis.proto`).
//!
//! * Authentication: mTLS (see `server`); the verified client certificate's
//!   identity is mapped to roles (`identity`). Every RPC requires a role;
//!   unknown identities have none (default deny). `AEGIS_INSECURE_DEV=1` (never
//!   in production) skips authentication and treats the caller as
//!   `insecure-dev`.
//! * Bounded concurrency: at most `max_concurrency` unary RPCs run at once;
//!   excess calls fail fast with RESOURCE_EXHAUSTED (the client treats any
//!   non-OK status as REJECT).
//! * Deadlines: every unary RPC is capped (`submit_timeout` for SubmitSignal,
//!   `rpc_timeout` otherwise); DEADLINE_EXCEEDED is a reject for the caller
//!   and a late approval is worthless because its attestation expires.
//! * Engine work is synchronous and does file I/O, so it runs on the blocking
//!   pool.

// `tonic::Status` is large by design and is the error type of every handler.
#![allow(clippy::result_large_err)]

use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Semaphore;
use tokio_stream::wrappers::WatchStream;
use tokio_stream::{Stream, StreamExt};
use tonic::{Request, Response, Status};

use crate::audit::AuditEvent;
use crate::engine::{Engine, HoldError};
use crate::identity::{cert_identity, Identities, PeerRole};
use crate::killswitch::controller::ControllerError;
use crate::killswitch::{ResetRefusal, ResetRequest};
use crate::pb;

const MAX_TEXT_LEN: usize = 512;
const MAX_APPROVALS: usize = 16;
const INSECURE_DEV_CALLER: &str = "insecure-dev";

#[derive(Debug, Clone)]
pub struct ServiceOptions {
    pub insecure_dev: bool,
    pub max_concurrency: usize,
    pub max_watchers: usize,
    pub submit_timeout: Duration,
    pub rpc_timeout: Duration,
}

pub struct AegisService {
    engine: Arc<Engine>,
    identities: Arc<Identities>,
    opts: ServiceOptions,
    permits: Arc<Semaphore>,
    watch_permits: Arc<Semaphore>,
}

struct Caller {
    id: String,
}

impl AegisService {
    pub fn new(
        engine: Arc<Engine>,
        identities: Arc<Identities>,
        opts: ServiceOptions,
    ) -> AegisService {
        AegisService {
            permits: Arc::new(Semaphore::new(opts.max_concurrency)),
            watch_permits: Arc::new(Semaphore::new(opts.max_watchers)),
            engine,
            identities,
            opts,
        }
    }

    fn authorize<T>(&self, req: &Request<T>, role: PeerRole) -> Result<Caller, Status> {
        if self.opts.insecure_dev {
            return Ok(Caller {
                id: INSECURE_DEV_CALLER.to_owned(),
            });
        }
        let unauth = || Status::unauthenticated("a valid client certificate is required");
        let certs = req.peer_certs().ok_or_else(unauth)?;
        let der = certs.first().ok_or_else(unauth)?;
        let id = cert_identity(der.as_ref()).ok_or_else(unauth)?;
        if !self.identities.roles_of(&id).contains(&role) {
            tracing::warn!(peer = %id, ?role, "rpc refused: peer lacks the required role");
            return Err(Status::permission_denied(
                "peer is not authorised for this rpc",
            ));
        }
        Ok(Caller { id })
    }

    /// Run engine work on the blocking pool under the concurrency bound and a deadline.
    async fn run<T, F>(&self, limit: Duration, work: F) -> Result<T, Status>
    where
        T: Send + 'static,
        F: FnOnce() -> T + Send + 'static,
    {
        let _permit = self
            .permits
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("aegis is at its concurrency limit"))?;
        match tokio::time::timeout(limit, tokio::task::spawn_blocking(work)).await {
            Ok(Ok(v)) => Ok(v),
            Ok(Err(_)) => Err(Status::internal("worker failed")),
            Err(_) => Err(Status::deadline_exceeded("aegis deadline exceeded")),
        }
    }

    /// Bind a request-supplied operator id to the authenticated caller.
    fn bind_operator(&self, caller: &Caller, claimed: &str) -> Result<(), Status> {
        if self.opts.insecure_dev || claimed == caller.id {
            Ok(())
        } else {
            Err(Status::permission_denied(
                "operator_id must match the authenticated identity",
            ))
        }
    }
}

fn check_len(name: &str, v: &str) -> Result<(), Status> {
    if v.len() > MAX_TEXT_LEN {
        return Err(Status::invalid_argument(format!("{name} too long")));
    }
    Ok(())
}

fn refusal_response(
    kill: &crate::killswitch::controller::KillController,
    why: String,
) -> pb::ResetKillSwitchResponse {
    pb::ResetKillSwitchResponse {
        accepted: false,
        refusal_reason: why,
        state: Some(kill.state()),
    }
}

fn do_reset(
    engine: &Engine,
    ids: &Identities,
    caller: &str,
    req: pb::ResetKillSwitchRequest,
) -> pb::ResetKillSwitchResponse {
    let kill = &engine.deps.kill;
    let now = engine.deps.clock.now_ns().unwrap_or(0);
    let max_age = engine.deps.limits.config.timings.approval_max_age_ms;
    let refuse = |why: ResetRefusal| {
        let event = AuditEvent::ResetRefused {
            trigger_id: req.trigger_id.clone(),
            caller: caller.to_owned(),
            reason: why.to_string(),
            at_ns: now,
        };
        if let Err(e) = engine.deps.audit.record(&event) {
            tracing::error!(error = %e, "audit sink failed for a refused reset");
        }
        refusal_response(kill, why.to_string())
    };
    if req.approvals.len() > MAX_APPROVALS {
        return refuse(ResetRefusal::Unauthenticated("too many approvals".into()));
    }
    let mut approvals = Vec::with_capacity(req.approvals.len());
    for a in &req.approvals {
        match ids.verify_approval(&req.trigger_id, a, now, max_age) {
            Ok(v) => approvals.push(v),
            Err(why) => return refuse(why),
        }
    }
    let reset = ResetRequest {
        trigger_id: req.trigger_id.clone(),
        approvals,
        root_cause_ref: req.root_cause_ref.clone(),
    };
    match kill.reset(&reset, caller) {
        Ok(state) => pb::ResetKillSwitchResponse {
            accepted: true,
            refusal_reason: String::new(),
            state: Some(state),
        },
        Err(why) => refusal_response(kill, why.to_string()),
    }
}

type StateStream =
    Pin<Box<dyn Stream<Item = Result<pb::KillSwitchState, Status>> + Send + 'static>>;

#[tonic::async_trait]
impl pb::Aegis for AegisService {
    async fn submit_signal(
        &self,
        request: Request<pb::TradeSignal>,
    ) -> Result<Response<pb::AegisDecision>, Status> {
        self.authorize(&request, PeerRole::SignalSubmitter)?;
        let signal = request.into_inner();
        let engine = self.engine.clone();
        let d = self
            .run(self.opts.submit_timeout, move || {
                engine.submit_signal(&signal)
            })
            .await?;
        Ok(Response::new(d))
    }

    async fn resolve_hold(
        &self,
        request: Request<pb::ResolveHoldRequest>,
    ) -> Result<Response<pb::AegisDecision>, Status> {
        let caller = self.authorize(&request, PeerRole::HoldResolver)?;
        let req = request.into_inner();
        self.bind_operator(&caller, &req.operator_id)?;
        let engine = self.engine.clone();
        let out = self
            .run(self.opts.rpc_timeout, move || engine.resolve_hold(&req))
            .await?;
        match out {
            Ok(d) => Ok(Response::new(d)),
            Err(HoldError::NotFound) => Err(Status::not_found("unknown or expired hold_id")),
            Err(HoldError::Refused(why)) => Err(Status::failed_precondition(why)),
            Err(HoldError::Internal) => Err(Status::internal("internal error")),
        }
    }

    async fn trigger_kill_switch(
        &self,
        request: Request<pb::TriggerKillSwitchRequest>,
    ) -> Result<Response<pb::KillSwitchState>, Status> {
        let caller = self.authorize(&request, PeerRole::KillTrigger)?;
        let req = request.into_inner();
        for (n, v) in [
            ("reason", &req.reason),
            ("actor_id", &req.actor_id),
            ("evidence_ref", &req.evidence_ref),
        ] {
            check_len(n, v)?;
        }
        let actor = if req.actor_id.is_empty() {
            caller.id
        } else {
            format!("{}:{}", caller.id, req.actor_id)
        };
        let reason = if req.evidence_ref.is_empty() {
            req.reason
        } else {
            format!("{} [{}]", req.reason, req.evidence_ref)
        };
        let kill = self.engine.deps.kill.clone();
        let out = self
            .run(self.opts.rpc_timeout, move || {
                kill.trigger(req.level, &reason, &actor)
            })
            .await?;
        match out {
            Ok(state) => Ok(Response::new(state)),
            Err(ControllerError::TooManyLatches) => {
                Err(Status::resource_exhausted("too many active latches"))
            }
            Err(ControllerError::NotPersisted) => Err(Status::internal(
                "latched but not persisted; state forced to HARD",
            )),
        }
    }

    async fn reset_kill_switch(
        &self,
        request: Request<pb::ResetKillSwitchRequest>,
    ) -> Result<Response<pb::ResetKillSwitchResponse>, Status> {
        let caller = self.authorize(&request, PeerRole::KillReset)?;
        let req = request.into_inner();
        check_len("trigger_id", &req.trigger_id)?;
        check_len("root_cause_ref", &req.root_cause_ref)?;
        let (engine, ids) = (self.engine.clone(), self.identities.clone());
        let out = self
            .run(self.opts.rpc_timeout, move || {
                do_reset(&engine, &ids, &caller.id, req)
            })
            .await?;
        Ok(Response::new(out))
    }

    async fn heartbeat(
        &self,
        request: Request<pb::HeartbeatRequest>,
    ) -> Result<Response<pb::KillSwitchState>, Status> {
        let caller = self.authorize(&request, PeerRole::Operator)?;
        let req = request.into_inner();
        if req.operator_id.is_empty() {
            return Err(Status::invalid_argument("operator_id required"));
        }
        self.bind_operator(&caller, &req.operator_id)?;
        let kill = self.engine.deps.kill.clone();
        let state = self
            .run(self.opts.rpc_timeout, move || {
                kill.heartbeat(&req.operator_id)
            })
            .await?;
        Ok(Response::new(state))
    }

    async fn get_kill_switch_state(
        &self,
        request: Request<pb::Empty>,
    ) -> Result<Response<pb::KillSwitchState>, Status> {
        self.authorize(&request, PeerRole::StateReader)?;
        Ok(Response::new(self.engine.deps.kill.state()))
    }

    type WatchKillSwitchStateStream = StateStream;

    async fn watch_kill_switch_state(
        &self,
        request: Request<pb::Empty>,
    ) -> Result<Response<StateStream>, Status> {
        self.authorize(&request, PeerRole::StateReader)?;
        let permit = self
            .watch_permits
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("too many watchers"))?;
        let rx = self.engine.deps.kill.subscribe();
        // The first item is the current state; the permit lives as long as the stream.
        let stream = WatchStream::new(rx).map(move |s| {
            let _held = &permit;
            Ok(s)
        });
        Ok(Response::new(Box::pin(stream)))
    }

    async fn get_aegis_state(
        &self,
        request: Request<pb::Empty>,
    ) -> Result<Response<pb::AegisState>, Status> {
        self.authorize(&request, PeerRole::StateReader)?;
        let engine = self.engine.clone();
        let st = self
            .run(self.opts.rpc_timeout, move || engine.aegis_state())
            .await?;
        Ok(Response::new(st))
    }

    async fn report_execution(
        &self,
        request: Request<pb::ExecutionReport>,
    ) -> Result<Response<pb::Ack>, Status> {
        self.authorize(&request, PeerRole::ExecutionReporter)?;
        let report = request.into_inner();
        let engine = self.engine.clone();
        let ack = self
            .run(self.opts.rpc_timeout, move || {
                engine.report_execution(&report)
            })
            .await?;
        Ok(Response::new(ack))
    }
}
