//! Structured audit records (spec sections 4.2 item 5 and 9).
//!
//! Aegis emits one record per decision and per kill-switch transition through
//! the [`AuditSink`] trait so the Zone C audit logger can subscribe later.
//! Records carry identifiers, enum names, thresholds and observed values only:
//! no LLM text (`debate_summary`), no key material, no credentials, no PII.
//!
//! When the sink is configured as mandatory (`audit_mandatory` in the limits
//! file) a sink error fails the decision closed (`REASON_AUDIT_UNAVAILABLE`).

use std::fs::{File, OpenOptions};
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;

use serde::Serialize;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AuditError {
    #[error("audit sink unavailable: {0}")]
    Unavailable(String),
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ControlAudit {
    pub id: String,
    pub hard: bool,
    pub passed: bool,
    pub reason: String,
    pub threshold: String,
    pub observed: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct DecisionAudit {
    pub signal_id: String,
    pub symbol: String,
    pub decision: String,
    pub signal_status: String,
    pub reasons: Vec<String>,
    pub controls: Vec<ControlAudit>,
    pub order_id: String,
    pub attestation_key_id: String,
    pub kill_state_seq: u64,
    pub limits_sha256: String,
    pub aegis_version: String,
    pub decided_at_ns: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct KillAudit {
    /// `latch`, `reset`, `heartbeat`, `heartbeat_warning`, `startup_fail_closed`.
    pub kind: String,
    pub trigger_id: String,
    pub actor_id: String,
    pub reason: String,
    pub prev_level: String,
    pub new_level: String,
    pub state_seq: u64,
    pub at_ns: i64,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum AuditEvent {
    Decision(DecisionAudit),
    KillTransition(KillAudit),
    ResetRefused {
        trigger_id: String,
        caller: String,
        reason: String,
        at_ns: i64,
    },
    HoldResolved {
        hold_id: String,
        signal_id: String,
        operator_id: String,
        second_approver_id: String,
        approved: bool,
        outcome: String,
        at_ns: i64,
    },
    Security {
        kind: String,
        detail: String,
        at_ns: i64,
    },
}

pub trait AuditSink: Send + Sync {
    fn record(&self, event: &AuditEvent) -> Result<(), AuditError>;
    /// Cheap health probe for `GetAegisState.audit_sink_ok`.
    fn is_healthy(&self) -> bool {
        true
    }
}

/// Default sink: structured JSON line through `tracing` (never fails).
#[derive(Debug, Default)]
pub struct TracingSink;

impl AuditSink for TracingSink {
    fn record(&self, event: &AuditEvent) -> Result<(), AuditError> {
        match serde_json::to_string(event) {
            Ok(line) => {
                tracing::info!(target: "aegis::audit", record = %line, "audit");
                Ok(())
            }
            Err(e) => Err(AuditError::Unavailable(format!("serialise: {e}"))),
        }
    }
}

/// Durable append-only JSON-lines WAL, fsynced per record (spec 9: no
/// un-audited decision). fsync policy is TODO(owner) (spec section 8).
#[derive(Debug)]
pub struct FileWalSink {
    file: Mutex<File>,
}

impl FileWalSink {
    pub fn open(path: &Path) -> Result<FileWalSink, AuditError> {
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|e| AuditError::Unavailable(format!("open wal: {e}")))?;
        Ok(FileWalSink {
            file: Mutex::new(file),
        })
    }
}

impl AuditSink for FileWalSink {
    fn record(&self, event: &AuditEvent) -> Result<(), AuditError> {
        let mut line = serde_json::to_vec(event)
            .map_err(|e| AuditError::Unavailable(format!("serialise: {e}")))?;
        line.push(b'\n');
        let mut file = self
            .file
            .lock()
            .map_err(|_| AuditError::Unavailable("wal lock poisoned".into()))?;
        file.write_all(&line)
            .and_then(|()| file.sync_data())
            .map_err(|e| AuditError::Unavailable(format!("wal write: {e}")))
    }
}

/// Sends every record to all sinks; reports the first error but still tries all.
pub struct FanoutSink(pub Vec<std::sync::Arc<dyn AuditSink>>);

impl AuditSink for FanoutSink {
    fn record(&self, event: &AuditEvent) -> Result<(), AuditError> {
        let mut first_err = None;
        for sink in &self.0 {
            if let Err(e) = sink.record(event) {
                first_err.get_or_insert(e);
            }
        }
        first_err.map_or(Ok(()), Err)
    }

    fn is_healthy(&self) -> bool {
        self.0.iter().all(|s| s.is_healthy())
    }
}

/// Sink used at start-up: the kill-switch controller must load its state
/// BEFORE the audit WAL file exists in the state directory (a missing kill
/// state next to other files means "lost", i.e. HARD), so early records are
/// buffered here and flushed when the real sink is attached.
pub struct DeferredSink {
    inner: Mutex<Deferred>,
}

enum Deferred {
    Buffering(Vec<AuditEvent>),
    Attached(std::sync::Arc<dyn AuditSink>),
}

/// Bound on records buffered before the real sink is attached.
const MAX_DEFERRED: usize = 1_024;

impl DeferredSink {
    pub fn new() -> DeferredSink {
        DeferredSink {
            inner: Mutex::new(Deferred::Buffering(Vec::new())),
        }
    }

    /// Attach the real sink and flush what was buffered. A flush failure is
    /// returned and the sink stays attached (later records still flow).
    pub fn attach(&self, sink: std::sync::Arc<dyn AuditSink>) -> Result<(), AuditError> {
        let mut g = self
            .inner
            .lock()
            .map_err(|_| AuditError::Unavailable("deferred sink poisoned".into()))?;
        let buffered = match std::mem::replace(&mut *g, Deferred::Attached(sink.clone())) {
            Deferred::Buffering(v) => v,
            Deferred::Attached(_) => Vec::new(),
        };
        buffered.iter().try_for_each(|e| sink.record(e))
    }
}

impl Default for DeferredSink {
    fn default() -> Self {
        DeferredSink::new()
    }
}

impl AuditSink for DeferredSink {
    fn record(&self, event: &AuditEvent) -> Result<(), AuditError> {
        let mut g = self
            .inner
            .lock()
            .map_err(|_| AuditError::Unavailable("deferred sink poisoned".into()))?;
        match &mut *g {
            Deferred::Buffering(v) if v.len() < MAX_DEFERRED => {
                v.push(event.clone());
                Ok(())
            }
            Deferred::Buffering(_) => {
                Err(AuditError::Unavailable("start-up audit buffer full".into()))
            }
            Deferred::Attached(s) => s.record(event),
        }
    }

    fn is_healthy(&self) -> bool {
        match self.inner.lock() {
            Ok(g) => match &*g {
                Deferred::Buffering(_) => true,
                Deferred::Attached(s) => s.is_healthy(),
            },
            Err(_) => false,
        }
    }
}

/// In-memory sink for tests and embedding.
#[derive(Debug, Default)]
pub struct MemorySink {
    events: Mutex<Vec<AuditEvent>>,
}

impl MemorySink {
    pub fn events(&self) -> Vec<AuditEvent> {
        self.events.lock().map(|e| e.clone()).unwrap_or_default()
    }
}

impl AuditSink for MemorySink {
    fn record(&self, event: &AuditEvent) -> Result<(), AuditError> {
        self.events
            .lock()
            .map_err(|_| AuditError::Unavailable("poisoned".into()))?
            .push(event.clone());
        Ok(())
    }
}

/// Sink that always fails (tests for the fail-closed path).
#[derive(Debug, Default)]
pub struct FailingSink;

impl AuditSink for FailingSink {
    fn record(&self, _event: &AuditEvent) -> Result<(), AuditError> {
        Err(AuditError::Unavailable("configured to fail".into()))
    }

    fn is_healthy(&self) -> bool {
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sec() -> AuditEvent {
        AuditEvent::Security {
            kind: "replay_mismatch".into(),
            detail: "signal 1".into(),
            at_ns: 1,
        }
    }

    #[test]
    fn wal_appends_json_lines_and_survives_reopen() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("audit.wal");
        {
            let wal = FileWalSink::open(&path).unwrap();
            wal.record(&sec()).unwrap();
            wal.record(&sec()).unwrap();
        }
        FileWalSink::open(&path).unwrap().record(&sec()).unwrap();
        let text = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = text.lines().collect();
        assert_eq!(lines.len(), 3);
        let v: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(v["event"], "security");
    }

    #[test]
    fn fanout_reports_error_but_delivers_to_healthy_sinks() {
        let mem = std::sync::Arc::new(MemorySink::default());
        let fan = FanoutSink(vec![std::sync::Arc::new(FailingSink), mem.clone()]);
        assert!(fan.record(&sec()).is_err());
        assert_eq!(mem.events().len(), 1);
        assert!(!fan.is_healthy());
    }

    #[test]
    fn deferred_sink_buffers_then_flushes_in_order_and_then_forwards() {
        let d = DeferredSink::new();
        d.record(&sec()).unwrap();
        let mut second = sec();
        if let AuditEvent::Security { detail, .. } = &mut second {
            *detail = "second".into();
        }
        d.record(&second).unwrap();
        let mem = std::sync::Arc::new(MemorySink::default());
        d.attach(mem.clone()).unwrap();
        d.record(&sec()).unwrap();
        let events = mem.events();
        assert_eq!(events.len(), 3);
        assert_eq!(events[1], second);
        assert!(d.is_healthy());
    }

    #[test]
    fn deferred_sink_buffer_is_bounded_and_flush_errors_surface() {
        let d = DeferredSink::new();
        for _ in 0..MAX_DEFERRED {
            d.record(&sec()).unwrap();
        }
        assert!(d.record(&sec()).is_err());
        assert!(d.attach(std::sync::Arc::new(FailingSink)).is_err());
        assert!(!d.is_healthy());
    }

    #[test]
    fn tracing_sink_never_fails() {
        assert!(TracingSink.record(&sec()).is_ok());
    }
}
