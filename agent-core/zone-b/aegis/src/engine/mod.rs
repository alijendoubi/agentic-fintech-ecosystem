//! The Aegis decision engine: glues validation, the pure control pipeline,
//! kill-switch state, replay protection, exposure tracking, signing and audit.
//!
//! All decisions are serialised through one mutex (`Core`). The decision path
//! is a few microseconds of integer arithmetic plus, on approval, one signature
//! and two small fsyncs, so serialising is simpler and safer than fine-grained
//! locking: exposure reservations, rate tokens and the replay check can never
//! race. Lock order is always `core` -> kill controller; the kill controller
//! never calls back into the engine.
//!
//! Failure policy: every error path yields REJECTED (or HELD_FOR_HUMAN where
//! the spec says so). A rejection is never turned into an approval.

mod build;
mod hold;
mod report;
mod submit;

use std::sync::{Arc, Mutex, MutexGuard};

use crate::audit::AuditSink;
use crate::clock::Clock;
use crate::controls::{EquityView, ExposureView, RateView, ReplayVerdict, SizeHistory, Snapshot};
use crate::domain::{Mode, ValidatedSignal};
use crate::killswitch::controller::KillController;
use crate::limits::Limits;
use crate::pb;
use crate::signing::Signer;
use crate::state::holds::HoldStore;
use crate::state::portfolio::{Portfolio, PortfolioStore};
use crate::state::rate::RateLimiter;
use crate::state::refdata::ReferenceData;
use crate::state::replay::ReplayStore;

pub use hold::HoldError;

/// Dependencies, injected so tests can substitute every side effect.
#[derive(Clone)]
pub struct EngineDeps {
    pub limits: Arc<Limits>,
    pub clock: Arc<dyn Clock>,
    pub kill: Arc<KillController>,
    pub signer: Arc<dyn Signer>,
    pub audit: Arc<dyn AuditSink>,
    pub replay: Arc<dyn ReplayStore>,
    pub portfolio_store: Arc<dyn PortfolioStore>,
    pub refdata: Arc<dyn ReferenceData>,
}

pub(crate) struct Core {
    /// `None` = the portfolio could not be loaded: exposure and equity are
    /// unavailable and every approval path fails closed.
    pub portfolio: Option<Portfolio>,
    pub rate: RateLimiter,
    pub holds: HoldStore,
}

pub struct Engine {
    pub(crate) deps: EngineDeps,
    core: Mutex<Core>,
}

impl Engine {
    pub fn new(deps: EngineDeps) -> Engine {
        let now = deps.clock.now_ns().unwrap_or(0);
        let portfolio = match deps.portfolio_store.load() {
            Ok(p) => Some(p),
            Err(e) => {
                tracing::error!(error = %e, "portfolio state unreadable; all approvals will fail closed");
                None
            }
        };
        let cfg = &deps.limits.config;
        let core = Core {
            portfolio,
            rate: RateLimiter::new(cfg.rate_global, cfg.rate_per_symbol, now),
            holds: HoldStore::default(),
        };
        Engine {
            deps,
            core: Mutex::new(core),
        }
    }

    /// The kill-switch controller this engine consults.
    pub fn kill(&self) -> &Arc<KillController> {
        &self.deps.kill
    }

    pub(crate) fn lock(&self) -> Option<MutexGuard<'_, Core>> {
        self.core.lock().ok()
    }

    pub(crate) fn now(&self) -> Option<i64> {
        self.deps.clock.now_ns().ok()
    }

    /// Assemble the read-only view the controls evaluate. Consumes a rate
    /// token when `take_rate` (only for a real evaluation).
    pub(crate) fn snapshot<'a>(
        &'a self,
        core: &mut Core,
        sig: &ValidatedSignal,
        now_ns: i64,
        mode: Mode,
        replay: ReplayVerdict,
    ) -> Snapshot<'a> {
        let cfg = &self.deps.limits.config;
        let mark = |s: &str| self.deps.refdata.price(s).map(|p| p.mid);
        let exposure: Option<ExposureView> = core
            .portfolio
            .as_ref()
            .map(|p| p.exposure_view(&sig.symbol, &mark));
        let equity = core
            .portfolio
            .as_ref()
            .and_then(|p| p.equity_today(now_ns))
            .map(|(day_start, current)| EquityView { day_start, current });
        let history = core.portfolio.as_ref().map_or(
            SizeHistory {
                count: 0,
                p95: None,
            },
            |p| p.size_history(&sig.symbol, now_ns, cfg.timings.unusual_size_window_days),
        );
        let rate = if replay == ReplayVerdict::New {
            core.rate.check_and_take(&sig.symbol, now_ns)
        } else {
            RateView {
                global_ok: true,
                symbol_ok: true,
            }
        };
        Snapshot {
            limits: cfg,
            mode,
            now_ns,
            kill: self.deps.kill.effective_level(),
            replay,
            ref_price: self.deps.refdata.price(&sig.symbol),
            regime: self.deps.refdata.regime(),
            exposure,
            equity,
            rate,
            history,
        }
    }

    /// Periodic housekeeping: release expired reservations and expire holds.
    pub fn maintenance(&self) {
        let Some(now) = self.now() else { return };
        let Some(mut core) = self.lock() else { return };
        if let Some(p) = core.portfolio.as_mut() {
            p.expire_reservations(now);
        }
        for held in core.holds.take_expired(now) {
            self.expire_hold(&held, now);
        }
    }

    pub fn aegis_state(&self) -> pb::AegisState {
        let open_holds = self
            .lock()
            .map_or(0, |c| i32::try_from(c.holds.len()).unwrap_or(i32::MAX));
        pb::AegisState {
            kill: Some(self.deps.kill.state()),
            limits_config_sha256: self.deps.limits.sha256_hex.clone(),
            hsm_ok: self.deps.signer.is_healthy(),
            audit_sink_ok: self.deps.audit.is_healthy(),
            reference_data_fresh: self.deps.refdata.has_fresh_data(),
            open_holds,
            build_version: crate::VERSION.to_owned(),
        }
    }
}

#[cfg(test)]
mod tests;
