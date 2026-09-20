//! Aegis: the deterministic gate between LLM trade signals and the market.
//!
//! Default deny. Every failure, missing configuration, parse error, expired
//! signal, unknown enum, missing kill-switch state, replay or timeout results
//! in REJECT (or HELD_FOR_HUMAN where the Phase 3 spec says so). Money is
//! fixed-point int64 nanos; there is no `f64` money on the decision path.

pub mod audit;
pub mod clock;
pub mod config;
pub mod controls;
pub mod domain;
pub mod engine;
pub mod error;
pub mod hex;
pub mod killswitch;
pub mod limits;
pub mod money;
pub mod pb;
pub mod signing;
pub mod state;
#[cfg(any(test, feature = "testkit"))]
pub mod testkit;
pub mod validate;

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
