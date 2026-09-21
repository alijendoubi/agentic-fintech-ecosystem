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
pub mod ingest;
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

/// Whether a state file that is absent may be created from scratch.
///
/// A missing file next to other state is loss or tampering, never a first
/// boot: only [`StateInit::Bootstrap`] (decided once at start-up, when the
/// state dir was entirely empty) allows an absent file to be initialised empty.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StateInit {
    /// First boot of an empty state dir: create the file.
    Bootstrap,
    /// The state dir has existed before: a missing file is an error.
    Existing,
}

/// fsync a directory so a rename inside it survives a crash. A no-op where
/// directories cannot be opened for sync (non-unix).
pub(crate) fn sync_dir(dir: &std::path::Path) -> std::io::Result<()> {
    #[cfg(unix)]
    {
        std::fs::File::open(dir)?.sync_all()
    }
    #[cfg(not(unix))]
    {
        let _ = dir;
        Ok(())
    }
}
