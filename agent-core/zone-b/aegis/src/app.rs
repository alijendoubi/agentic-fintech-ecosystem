//! Process assembly: configuration -> state -> engine -> gRPC server.
//!
//! Start-up order matters (fail closed):
//!  1. Limits and identities must load, else refuse to start.
//!  2. The kill-switch state is loaded BEFORE any other file is created in the
//!     state dir (a missing kill state next to other files means HARD). Its
//!     early audit records are buffered by `DeferredSink` until the WAL opens.
//!  3. If the audit WAL cannot be opened and audit is mandatory, refuse to start.
//!  4. If the replay log or portfolio snapshot cannot be loaded (including
//!     MISSING next to other state files), latch HARD and reject every signal.
//!     Only an entirely empty state dir may bootstrap them empty.
//!  5. The signer must build (dev signer only outside production).

use std::sync::Arc;

use thiserror::Error;

use crate::audit::{AuditError, AuditSink, DeferredSink, FanoutSink, FileWalSink, TracingSink};
use crate::clock::{Clock, SystemClock};
use crate::config::{RuntimeConfig, SignerKind, TlsMode};
use crate::engine::{Engine, EngineDeps};
use crate::error::ConfigError;
use crate::identity::Identities;
use crate::killswitch::controller::KillController;
use crate::killswitch::store::FileKillStore;
use crate::killswitch::{HeartbeatConfig, STARTUP_ACTOR};
use crate::limits::Limits;
use crate::server::{load_tls, serve_on, shutdown_signal, spawn_ticker, ServeError};
use crate::service::{AegisService, ServiceOptions};
use crate::signing::dev::DevEd25519Signer;
use crate::signing::{SignError, Signer};
use crate::state::portfolio::FilePortfolioStore;
use crate::state::refdata::MemoryReferenceData;
use crate::state::replay::{FileReplayStore, ReplayStore, UnavailableReplayStore};
use crate::state::StateInit;

const MAX_WATCHERS: usize = 64;

#[derive(Debug, Error)]
pub enum StartupError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error("signer: {0}")]
    Sign(#[from] SignError),
    #[error("state directory: {0}")]
    StateDir(String),
    #[error(
        "state directory is empty: refusing a first boot in production. Set \
         AEGIS_ALLOW_FRESH_STATE=1 for the very first start only (an empty dir may \
         mean a wiped or wrongly mounted volume)"
    )]
    FreshStateNotAllowed,
    #[error("audit: {0}")]
    Audit(#[from] AuditError),
    #[error(transparent)]
    Serve(#[from] ServeError),
    #[error("AEGIS_SIGNER=pkcs11 requires a build with --features pkcs11")]
    Pkcs11NotCompiled,
}

/// A fully assembled Aegis, ready to serve.
pub struct App {
    pub engine: Arc<Engine>,
    /// Aegis-owned reference data, fed by the `PushReferenceData` RPC (the
    /// service is given this same store in `run`).
    pub refdata: Arc<MemoryReferenceData>,
    pub service_options: ServiceOptions,
    pub identities: Arc<Identities>,
}

fn prepare_state_dir(path: &std::path::Path) -> Result<(), StartupError> {
    std::fs::create_dir_all(path).map_err(|e| StartupError::StateDir(e.to_string()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
            .map_err(|e| StartupError::StateDir(e.to_string()))?;
    }
    Ok(())
}

fn build_signer(cfg: &RuntimeConfig) -> Result<Arc<dyn Signer>, StartupError> {
    match &cfg.signer {
        SignerKind::Dev { seed_file } => {
            let signer = match seed_file {
                Some(p) => DevEd25519Signer::from_seed_file(cfg.env, p)?,
                None => DevEd25519Signer::generate(cfg.env)?,
            };
            tracing::warn!(key_id = signer.key_id(), "DEV-ONLY software signer in use");
            Ok(Arc::new(signer))
        }
        #[cfg(feature = "pkcs11")]
        SignerKind::Pkcs11(c) => Ok(Arc::new(crate::signing::pkcs11::build_signer(c)?)),
        #[cfg(not(feature = "pkcs11"))]
        SignerKind::Pkcs11(_) => Err(StartupError::Pkcs11NotCompiled),
    }
}

fn open_audit(
    cfg: &RuntimeConfig,
    mandatory: bool,
    deferred: &DeferredSink,
) -> Result<(), StartupError> {
    let tracing_sink: Arc<dyn AuditSink> = Arc::new(TracingSink);
    let sink: Arc<dyn AuditSink> = match FileWalSink::open(&cfg.state_dir.join("audit.wal")) {
        Ok(wal) => Arc::new(FanoutSink(vec![tracing_sink, Arc::new(wal)])),
        Err(e) if mandatory => return Err(e.into()),
        Err(e) => {
            tracing::error!(error = %e, "audit WAL unavailable; continuing with tracing only (audit not mandatory)");
            tracing_sink
        }
    };
    deferred.attach(sink)?;
    Ok(())
}

/// Whether the state dir holds nothing at all. Must be sampled BEFORE any
/// store creates a file in it. An unreadable dir is an error (fail closed).
fn state_dir_is_empty(path: &std::path::Path) -> Result<bool, StartupError> {
    let mut entries = std::fs::read_dir(path).map_err(|e| StartupError::StateDir(e.to_string()))?;
    match entries.next() {
        None => Ok(true),
        Some(Ok(_)) => Ok(false),
        Some(Err(e)) => Err(StartupError::StateDir(e.to_string())),
    }
}

/// Force HARD (persisted) because durable state that must exist is unusable.
fn latch_hard_for_lost_state(kill: &KillController, what: &str, why: &str) {
    let reason = format!("start-up: {what} unavailable: {why}");
    tracing::error!(%reason, "forcing HARD");
    if let Err(e) = kill.trigger(
        crate::pb::KillSwitchLevel::KillLevelHard as i32,
        &reason,
        STARTUP_ACTOR,
    ) {
        tracing::error!(error = %e, "start-up HARD latch could not be persisted (held in memory)");
    }
}

/// Assemble everything or fail with a reason (the process then exits non-zero).
pub fn build_app(cfg: &RuntimeConfig) -> Result<App, StartupError> {
    let limits = Arc::new(Limits::from_path(&cfg.limits_file)?);
    let identities = Arc::new(Identities::from_path(&cfg.identities_file)?);
    prepare_state_dir(&cfg.state_dir)?;
    // Sampled before any store creates a file: only an entirely empty dir may
    // bootstrap empty portfolio / replay state.
    let state_init = if state_dir_is_empty(&cfg.state_dir)? {
        if cfg.env.is_production() && !cfg.allow_fresh_state {
            return Err(StartupError::FreshStateNotAllowed);
        }
        StateInit::Bootstrap
    } else {
        StateInit::Existing
    };
    let clock: Arc<dyn Clock> = Arc::new(SystemClock);
    let t = &limits.config.timings;
    let mandatory = limits.config.audit_mandatory;

    let deferred = Arc::new(DeferredSink::new());
    let kill = Arc::new(KillController::start(
        Arc::new(FileKillStore::new(&cfg.state_dir)),
        deferred.clone(),
        clock.clone(),
        HeartbeatConfig::from_ms(
            t.operator_heartbeat_interval_ms,
            t.operator_heartbeat_warn_ms,
        ),
        mandatory,
    ));
    open_audit(cfg, mandatory, &deferred)?;

    let now = clock.now_ns().unwrap_or(0);
    let replay: Arc<dyn ReplayStore> = match FileReplayStore::open(
        &cfg.state_dir,
        t.replay_retention_ms,
        now,
        state_init,
    ) {
        Ok(s) => Arc::new(s),
        Err(e) => {
            tracing::error!(error = %e, "replay store unavailable: every signal will be rejected");
            latch_hard_for_lost_state(&kill, "replay log", &e.to_string());
            Arc::new(UnavailableReplayStore)
        }
    };
    let refdata = Arc::new(MemoryReferenceData::default());
    let engine = Arc::new(Engine::new(EngineDeps {
        limits: limits.clone(),
        clock,
        kill: kill.clone(),
        signer: build_signer(cfg)?,
        audit: deferred,
        replay,
        portfolio_store: Arc::new(FilePortfolioStore::new(&cfg.state_dir, state_init)),
        refdata: refdata.clone(),
    }));
    if let Some(why) = engine.portfolio_load_error() {
        latch_hard_for_lost_state(&kill, "portfolio snapshot", &why);
    }
    Ok(App {
        engine,
        refdata,
        service_options: ServiceOptions {
            insecure_dev: matches!(cfg.tls, TlsMode::InsecureDev),
            max_concurrency: cfg.max_concurrency,
            max_watchers: MAX_WATCHERS,
            submit_timeout: cfg.submit_timeout,
            rpc_timeout: cfg.rpc_timeout,
        },
        identities,
    })
}

/// Build, bind and serve until SIGINT/SIGTERM.
pub async fn run(cfg: RuntimeConfig) -> Result<(), StartupError> {
    let app = build_app(&cfg)?;
    let tls = match &cfg.tls {
        TlsMode::Mutual(paths) => Some(load_tls(paths)?),
        TlsMode::InsecureDev => None,
    };
    let listener = tokio::net::TcpListener::bind(cfg.listen)
        .await
        .map_err(ServeError::Listen)?;
    tracing::info!(
        listen = %cfg.listen,
        limits_sha256 = %app.engine.deps.limits.sha256_hex,
        mtls = tls.is_some(),
        version = crate::VERSION,
        "aegis serving"
    );
    let _ticker = spawn_ticker(app.engine.clone());
    let service = AegisService::new(app.engine, app.identities, app.service_options)
        .with_refdata(app.refdata);
    serve_on(
        listener,
        tls,
        service,
        cfg.rpc_timeout.max(cfg.submit_timeout),
        shutdown_signal(),
    )
    .await?;
    Ok(())
}
