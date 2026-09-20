//! Aegis-owned state: replay protection, portfolio (positions, pending
//! orders, day equity, order-size history), rate limiting, held signals and
//! reference data.
//!
//! Spec 4.3 requires replay state to be DURABLE and not to live in an
//! LRU-evicting cache: [`replay::FileReplayStore`] is an fsynced append-only
//! log on the Aegis volume and [`replay::MemoryReplayStore`] never evicts.
//! Portfolio state is persisted as an atomically replaced JSON snapshot
//! ([`portfolio::FilePortfolioStore`]).

pub mod holds;
pub mod portfolio;
pub mod rate;
pub mod refdata;
pub mod replay;

use thiserror::Error;

#[derive(Debug, Error)]
pub enum StateError {
    #[error("state store unavailable: {0}")]
    Unavailable(String),
}
