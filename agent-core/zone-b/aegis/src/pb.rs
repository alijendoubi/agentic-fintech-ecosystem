//! Generated protobuf/gRPC types (package `afe.shared`) plus small helpers for
//! the decision types defined in `aegis.proto`.

#![allow(clippy::all)]

tonic::include_proto!("afe.shared");

pub use aegis_server::{Aegis, AegisServer};

impl ControlResult {
    /// A passed control.
    pub fn pass(
        id: &str,
        is_hard: bool,
        threshold: impl Into<String>,
        observed: impl Into<String>,
    ) -> Self {
        ControlResult {
            control_id: id.to_owned(),
            is_hard,
            passed: true,
            reason: ReasonCode::ReasonUnspecified as i32,
            threshold: threshold.into(),
            observed: observed.into(),
            detail: String::new(),
        }
    }

    /// A failed control with its reason code.
    pub fn fail(
        id: &str,
        is_hard: bool,
        reason: ReasonCode,
        threshold: impl Into<String>,
        observed: impl Into<String>,
    ) -> Self {
        ControlResult {
            control_id: id.to_owned(),
            is_hard,
            passed: false,
            reason: reason as i32,
            threshold: threshold.into(),
            observed: observed.into(),
            detail: String::new(),
        }
    }

    pub fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = detail.into();
        self
    }
}

impl KillSwitchLevel {
    /// Strict decode: unknown numbers are `None`, never silently NORMAL.
    pub fn from_wire(v: i32) -> Option<KillSwitchLevel> {
        KillSwitchLevel::try_from(v).ok()
    }
}
