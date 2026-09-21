//! Independent Supervisor (spec 5.3 item 2): `aegis supervisor`.
//!
//! A separate PROCESS (same binary, its own entry point) that probes Aegis over
//! mTLS and, once the probes have failed continuously for the trip window
//! (60 s), calls `TriggerKillSwitch(HARD, actor "supervisor/liveness")`. It has
//! to be independent because a hung Aegis cannot report itself dead.
//!
//! Fail closed on its own errors: bad or missing configuration, unreadable TLS
//! files, an unreadable clock, or a trip that stays undeliverable for another
//! full window all END THE PROCESS with a non-zero exit code (the orchestrator
//! restarts it and alerts); it never stays silently alive without protecting.
//! A crash (panic aborts in release) is a non-zero exit as well.
//!
//! Not done here (owned elsewhere): cutting broker egress, and supervising the
//! Supervisor (run it under a restart policy and alert on its restarts).

pub mod config;
pub mod grpc;

use std::time::{Duration, Instant};

use thiserror::Error;

use crate::clock::{Clock, ClockError};
use crate::error::ConfigError;
use crate::killswitch::watchdog::{LivenessWatchdog, SUPERVISOR_ACTOR};

pub use config::SupervisorConfig;
pub use grpc::GrpcAegis;

/// One failed exchange with Aegis (probe or trip). Carries text only for logs.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
#[error("{0}")]
pub struct LinkError(pub String);

/// What the Supervisor needs from Aegis (a fake in unit tests, gRPC in prod).
#[tonic::async_trait]
pub trait AegisLink: Send + Sync {
    /// `Ok` only if Aegis answered a liveness probe correctly within its deadline.
    async fn probe(&self) -> Result<(), LinkError>;
    /// Latch HARD as [`SUPERVISOR_ACTOR`]. `Ok` only if Aegis confirms the
    /// effective level is at least HARD.
    async fn trip_hard(&self, reason: &str, evidence_ref: &str) -> Result<(), LinkError>;
}

#[derive(Debug, Error)]
pub enum SupervisorError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error("cannot set up the Aegis link: {0}")]
    Setup(String),
    #[error("the supervisor's own clock failed")]
    Clock(#[from] ClockError),
    #[error("HARD could not be delivered for {waited_ms} ms after liveness was lost")]
    TripUndeliverable { waited_ms: u64 },
}

impl SupervisorError {
    /// Process exit code: 2 configuration/setup, 3 trip undeliverable, 1 other.
    pub fn exit_code(&self) -> u8 {
        match self {
            SupervisorError::Config(_) | SupervisorError::Setup(_) => 2,
            SupervisorError::TripUndeliverable { .. } => 3,
            SupervisorError::Clock(_) => 1,
        }
    }
}

/// Monotonic clock (immune to wall-clock steps) for the liveness window.
#[derive(Debug)]
pub struct MonotonicClock {
    start: Instant,
}

impl MonotonicClock {
    pub fn new() -> MonotonicClock {
        MonotonicClock {
            start: Instant::now(),
        }
    }
}

impl Default for MonotonicClock {
    fn default() -> Self {
        MonotonicClock::new()
    }
}

impl Clock for MonotonicClock {
    fn now_ns(&self) -> Result<i64, ClockError> {
        // +1 keeps the first reading strictly positive.
        i64::try_from(self.start.elapsed().as_nanos())
            .map(|n| n.saturating_add(1))
            .map_err(|_| ClockError)
    }
}

/// What one supervision step observed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    /// The probe succeeded.
    Healthy,
    /// The probe failed; `failed_for_ms` since the last success.
    Degraded { failed_for_ms: u64 },
    /// HARD was just latched.
    Tripped,
    /// HARD is latched for this outage; still waiting for Aegis to recover.
    AlreadyTripped,
    /// The trip window elapsed but HARD could not be delivered (will retry).
    TripFailed { failed_for_ms: u64 },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Phase {
    Watching,
    /// The trip is due but undelivered since `since_ns`.
    TripPending {
        since_ns: i64,
    },
    Tripped,
}

pub struct Supervisor<L: AegisLink, C: Clock> {
    link: L,
    clock: C,
    watchdog: LivenessWatchdog,
    trip_after_ms: u64,
    last_ok_ns: i64,
    phase: Phase,
}

fn ns_to_ms(ns: i64) -> u64 {
    u64::try_from(ns.max(0) / 1_000_000).unwrap_or(u64::MAX)
}

impl<L: AegisLink, C: Clock> Supervisor<L, C> {
    /// Starts the window NOW: an Aegis that is down from the start is tripped
    /// `trip_after_ms` after the Supervisor came up.
    pub fn new(link: L, clock: C, trip_after_ms: u64) -> Result<Self, SupervisorError> {
        let now = clock.now_ns()?;
        Ok(Supervisor {
            link,
            clock,
            watchdog: LivenessWatchdog::new(now, trip_after_ms),
            trip_after_ms,
            last_ok_ns: now,
            phase: Phase::Watching,
        })
    }

    /// One probe (and, if the window has elapsed, one trip attempt).
    pub async fn step(&mut self) -> Result<Outcome, SupervisorError> {
        let probed = self.link.probe().await;
        let now = self.clock.now_ns()?;
        if let Err(e) = &probed {
            tracing::warn!(error = %e, "aegis liveness probe failed");
        } else {
            self.watchdog.observe_ok(now);
            self.last_ok_ns = self.last_ok_ns.max(now);
            if self.phase != Phase::Watching {
                tracing::warn!("aegis is answering again; supervisor re-armed");
            }
            self.phase = Phase::Watching;
            return Ok(Outcome::Healthy);
        }
        let failed_for_ms = ns_to_ms(now.saturating_sub(self.last_ok_ns));
        if !self.watchdog.should_trip(now) {
            return Ok(Outcome::Degraded { failed_for_ms });
        }
        self.trip(now, failed_for_ms).await
    }

    async fn trip(&mut self, now: i64, failed_for_ms: u64) -> Result<Outcome, SupervisorError> {
        if self.phase == Phase::Tripped {
            return Ok(Outcome::AlreadyTripped);
        }
        if let Phase::TripPending { since_ns } = self.phase {
            let waited_ms = ns_to_ms(now.saturating_sub(since_ns));
            if waited_ms > self.trip_after_ms {
                return Err(SupervisorError::TripUndeliverable { waited_ms });
            }
        }
        let reason = format!(
            "aegis liveness probes failing for {failed_for_ms} ms (window {} ms)",
            self.trip_after_ms
        );
        let evidence = format!("supervisor:failed_for_ms={failed_for_ms}");
        match self.link.trip_hard(&reason, &evidence).await {
            Ok(()) => {
                tracing::error!(%reason, "HARD kill switch latched by the supervisor");
                self.phase = Phase::Tripped;
                Ok(Outcome::Tripped)
            }
            Err(e) => {
                tracing::error!(error = %e, "cannot deliver HARD; retrying");
                if !matches!(self.phase, Phase::TripPending { .. }) {
                    self.phase = Phase::TripPending { since_ns: now };
                }
                Ok(Outcome::TripFailed { failed_for_ms })
            }
        }
    }

    /// Supervise until `shutdown` resolves (clean exit) or an own error occurs.
    pub async fn run<F>(mut self, interval: Duration, shutdown: F) -> Result<(), SupervisorError>
    where
        F: std::future::Future<Output = ()>,
    {
        tokio::pin!(shutdown);
        let mut ticker = tokio::time::interval(interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tokio::select! {
                _ = &mut shutdown => return Ok(()),
                _ = ticker.tick() => {}
            }
            self.step().await?;
        }
    }
}

/// Entry point of `aegis supervisor`: build from the environment and run until
/// SIGINT/SIGTERM. Every failure is an `Err` the caller turns into a non-zero
/// exit code.
pub async fn run_from_env() -> Result<(), SupervisorError> {
    let cfg = SupervisorConfig::from_env()?;
    run(cfg, crate::server::shutdown_signal()).await
}

pub async fn run<F>(cfg: SupervisorConfig, shutdown: F) -> Result<(), SupervisorError>
where
    F: std::future::Future<Output = ()>,
{
    let link = GrpcAegis::connect(&cfg)?;
    let supervisor = Supervisor::new(link, MonotonicClock::new(), cfg.trip_after_ms)?;
    tracing::info!(
        target = %cfg.target,
        trip_after_ms = cfg.trip_after_ms,
        actor = SUPERVISOR_ACTOR,
        version = crate::VERSION,
        "aegis supervisor running"
    );
    supervisor.run(cfg.probe_interval, shutdown).await
}

#[cfg(test)]
mod tests;
