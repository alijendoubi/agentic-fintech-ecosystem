//! Sensory Array (Zone B): Polygon.io L2 quotes/trades -> normalised
//! `MarketSnapshot`s -> QuestDB (ILP) + Redis (`sensory:snapshots`).
//!
//! The binary in `main.rs` is a thin wrapper; everything testable lives here.

pub mod config;
pub mod error;
pub mod ilp;
pub mod metrics;
pub mod normalizer;
pub mod polygon;
pub mod queue;
pub mod ttl;
pub mod validate;
