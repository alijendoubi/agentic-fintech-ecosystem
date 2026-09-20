//! Sensory Array (Zone B): Polygon.io L2 quotes/trades -> normalised
//! `MarketSnapshot`s -> QuestDB (ILP) + Redis (`sensory:snapshots`).
//!
//! The binary in `main.rs` is a thin wrapper; everything testable lives here.

pub mod config;
pub mod error;
pub mod health;
pub mod ilp;
#[cfg(feature = "ilp-secure")]
pub mod ilp_secure;
pub mod ingestor;
pub mod metrics;
pub mod normalizer;
pub mod polygon;
pub mod publisher;
pub mod questdb_writer;
pub mod queue;
pub mod regime;
pub mod runtime;
pub mod testutil;
pub mod ttl;
pub mod validate;
