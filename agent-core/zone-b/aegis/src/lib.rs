//! Aegis: the deterministic gate between LLM trade signals and the market.
//!
//! Default deny. Every failure, missing configuration, parse error, expired
//! signal, unknown enum, missing kill-switch state, replay or timeout results
//! in REJECT (or HELD_FOR_HUMAN where the Phase 3 spec says so). Money is
//! fixed-point int64 nanos; there is no `f64` money on the decision path.

pub mod config;
pub mod error;
pub mod hex;
pub mod limits;
pub mod money;
pub mod pb;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
