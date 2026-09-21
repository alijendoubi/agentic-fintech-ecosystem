//! Duplicate / replay protection (control C07, spec 4.3).
//!
//! A record holds the SHA-256 of the canonical request payload and the
//! decision that was returned. Same id + same payload => the ORIGINAL decision
//! is returned (never re-approved, never a second attestation). Same id +
//! different payload => `REASON_REPLAY_PAYLOAD_MISMATCH`.
//!
//! Records are retained for `replay_retention_ms` (PROPOSED 24 h) and are never
//! evicted for capacity. Any store failure is `StateError`, which the engine
//! maps to REJECT (`REASON_STATE_UNAVAILABLE`).

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};

use super::{sync_dir, StateError, StateInit};
use crate::hex;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ReplayRecord {
    pub signal_id: String,
    /// Hex SHA-256 of the canonical request payload.
    pub payload_sha256: String,
    /// Hex of the protobuf-encoded `AegisDecision` returned to the caller.
    pub decision_hex: String,
    pub recorded_at_ns: i64,
}

impl ReplayRecord {
    pub fn new(signal_id: &str, payload: &[u8; 32], decision: &[u8], at_ns: i64) -> ReplayRecord {
        ReplayRecord {
            signal_id: signal_id.to_owned(),
            payload_sha256: hex::encode(payload),
            decision_hex: hex::encode(decision),
            recorded_at_ns: at_ns,
        }
    }
}

pub trait ReplayStore: Send + Sync {
    fn lookup(&self, signal_id: &str) -> Result<Option<ReplayRecord>, StateError>;
    /// Insert or replace (a held signal's record is replaced by its final decision).
    fn record(&self, rec: &ReplayRecord) -> Result<(), StateError>;
    fn is_healthy(&self) -> bool {
        true
    }
}

fn poisoned() -> StateError {
    StateError::Unavailable("replay store lock poisoned".into())
}

/// Never evicts; purges only records older than the retention window.
#[derive(Debug)]
pub struct MemoryReplayStore {
    map: Mutex<HashMap<String, ReplayRecord>>,
    retention_ns: i64,
}

impl MemoryReplayStore {
    pub fn new(retention_ms: u64) -> MemoryReplayStore {
        MemoryReplayStore {
            map: Mutex::new(HashMap::new()),
            retention_ns: retention_ns(retention_ms),
        }
    }
}

fn retention_ns(ms: u64) -> i64 {
    i64::try_from(ms)
        .unwrap_or(i64::MAX / 1_000_000)
        .saturating_mul(1_000_000)
}

impl ReplayStore for MemoryReplayStore {
    fn lookup(&self, signal_id: &str) -> Result<Option<ReplayRecord>, StateError> {
        Ok(self
            .map
            .lock()
            .map_err(|_| poisoned())?
            .get(signal_id)
            .cloned())
    }

    fn record(&self, rec: &ReplayRecord) -> Result<(), StateError> {
        let mut map = self.map.lock().map_err(|_| poisoned())?;
        let cutoff = rec.recorded_at_ns.saturating_sub(self.retention_ns);
        map.retain(|_, r| r.recorded_at_ns >= cutoff);
        map.insert(rec.signal_id.clone(), rec.clone());
        Ok(())
    }
}

/// fsynced append-only JSON-lines log; the whole log is loaded into memory on
/// open (expired records dropped) and compacted by an atomic rewrite.
#[derive(Debug)]
pub struct FileReplayStore {
    inner: MemoryReplayStore,
    file: Mutex<File>,
}

const LOG_NAME: &str = "replay.log";

impl FileReplayStore {
    /// Open the log. A corrupt log is an ERROR, not an empty store, and so is a
    /// MISSING log unless `init` is [`StateInit::Bootstrap`] (first boot of an
    /// empty state dir): silently dropping replay history would defeat the
    /// control by letting every previously seen signal id be approved again.
    pub fn open(
        dir: &Path,
        retention_ms: u64,
        now_ns: i64,
        init: StateInit,
    ) -> Result<FileReplayStore, StateError> {
        let path = dir.join(LOG_NAME);
        let inner = MemoryReplayStore::new(retention_ms);
        let cutoff = now_ns.saturating_sub(retention_ns(retention_ms));
        let kept = load(&path, cutoff, init == StateInit::Bootstrap)?;
        rewrite(&path, dir, kept.values())?;
        {
            let mut map = inner.map.lock().map_err(|_| poisoned())?;
            *map = kept;
        }
        let file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&path)
            .map_err(|e| StateError::Unavailable(format!("open replay log: {e}")))?;
        Ok(FileReplayStore {
            inner,
            file: Mutex::new(file),
        })
    }
}

fn load(
    path: &PathBuf,
    cutoff_ns: i64,
    allow_missing: bool,
) -> Result<HashMap<String, ReplayRecord>, StateError> {
    let mut map = HashMap::new();
    let file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound && allow_missing => return Ok(map),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(StateError::Unavailable(
                "replay.log is missing from an existing state dir: refusing to start with \
                 empty replay history"
                    .into(),
            ))
        }
        Err(e) => return Err(StateError::Unavailable(format!("read replay log: {e}"))),
    };
    for (n, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|e| StateError::Unavailable(format!("read replay log: {e}")))?;
        if line.trim().is_empty() {
            continue;
        }
        let rec: ReplayRecord = serde_json::from_str(&line).map_err(|e| {
            StateError::Unavailable(format!("corrupt replay log line {}: {e}", n + 1))
        })?;
        if rec.recorded_at_ns >= cutoff_ns {
            map.insert(rec.signal_id.clone(), rec);
        }
    }
    Ok(map)
}

fn rewrite<'a>(
    path: &Path,
    dir: &Path,
    records: impl Iterator<Item = &'a ReplayRecord>,
) -> Result<(), StateError> {
    let err = |what: &str, e: std::io::Error| StateError::Unavailable(format!("{what}: {e}"));
    let tmp = dir.join("replay.log.tmp");
    let mut f = File::create(&tmp).map_err(|e| err("create tmp", e))?;
    for r in records {
        let mut line = serde_json::to_vec(r).map_err(|e| StateError::Unavailable(e.to_string()))?;
        line.push(b'\n');
        f.write_all(&line).map_err(|e| err("write", e))?;
    }
    f.sync_all().map_err(|e| err("fsync", e))?;
    fs::rename(&tmp, path).map_err(|e| err("rename", e))?;
    sync_dir(dir).map_err(|e| err("fsync dir", e))
}

impl ReplayStore for FileReplayStore {
    fn lookup(&self, signal_id: &str) -> Result<Option<ReplayRecord>, StateError> {
        self.inner.lookup(signal_id)
    }

    fn record(&self, rec: &ReplayRecord) -> Result<(), StateError> {
        let mut line =
            serde_json::to_vec(rec).map_err(|e| StateError::Unavailable(e.to_string()))?;
        line.push(b'\n');
        {
            let mut f = self.file.lock().map_err(|_| poisoned())?;
            f.write_all(&line)
                .and_then(|()| f.sync_data())
                .map_err(|e| StateError::Unavailable(format!("append replay log: {e}")))?;
        }
        self.inner.record(rec)
    }
}

/// Store that always fails: used when the durable store cannot be opened, so
/// every signal is rejected (replay protection cannot be guaranteed).
#[derive(Debug, Default)]
pub struct UnavailableReplayStore;

impl ReplayStore for UnavailableReplayStore {
    fn lookup(&self, _signal_id: &str) -> Result<Option<ReplayRecord>, StateError> {
        Err(StateError::Unavailable("replay store not open".into()))
    }

    fn record(&self, _rec: &ReplayRecord) -> Result<(), StateError> {
        Err(StateError::Unavailable("replay store not open".into()))
    }

    fn is_healthy(&self) -> bool {
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const DAY_MS: u64 = 24 * 3_600_000;
    const DAY_NS: i64 = 24 * 3_600_000_000_000;

    fn rec(id: &str, at: i64) -> ReplayRecord {
        ReplayRecord::new(id, &[1; 32], b"decision", at)
    }

    #[test]
    fn memory_store_never_evicts_within_retention_and_purges_after() {
        let s = MemoryReplayStore::new(DAY_MS);
        for i in 0..5_000 {
            s.record(&rec(&format!("id{i}"), 1_000 + i)).unwrap();
        }
        assert!(s.lookup("id0").unwrap().is_some(), "no capacity eviction");
        s.record(&rec("late", 1_000 + 2 * DAY_NS)).unwrap();
        assert!(s.lookup("id0").unwrap().is_none(), "expired by retention");
        assert!(s.lookup("late").unwrap().is_some());
    }

    #[test]
    fn record_replaces_a_held_decision_with_the_final_one() {
        let s = MemoryReplayStore::new(DAY_MS);
        s.record(&rec("a", 1)).unwrap();
        let mut fin = rec("a", 2);
        fin.decision_hex = hex::encode(b"final");
        s.record(&fin).unwrap();
        assert_eq!(
            s.lookup("a").unwrap().unwrap().decision_hex,
            hex::encode(b"final")
        );
    }

    #[test]
    fn file_store_survives_restart_and_takes_the_latest_record() {
        let dir = tempfile::tempdir().unwrap();
        {
            let s = FileReplayStore::open(dir.path(), DAY_MS, 100, StateInit::Bootstrap).unwrap();
            s.record(&rec("a", 100)).unwrap();
            let mut fin = rec("a", 101);
            fin.decision_hex = hex::encode(b"final");
            s.record(&fin).unwrap();
            s.record(&rec("b", 102)).unwrap();
        }
        let s = FileReplayStore::open(dir.path(), DAY_MS, 200, StateInit::Bootstrap).unwrap();
        assert_eq!(
            s.lookup("a").unwrap().unwrap().decision_hex,
            hex::encode(b"final")
        );
        assert!(s.lookup("b").unwrap().is_some());
        assert!(s.lookup("zzz").unwrap().is_none());
    }

    #[test]
    fn file_store_drops_expired_records_on_open() {
        let dir = tempfile::tempdir().unwrap();
        FileReplayStore::open(dir.path(), DAY_MS, 0, StateInit::Bootstrap)
            .unwrap()
            .record(&rec("old", 10))
            .unwrap();
        let s = FileReplayStore::open(dir.path(), DAY_MS, 10 + 2 * DAY_NS, StateInit::Bootstrap)
            .unwrap();
        assert!(s.lookup("old").unwrap().is_none());
    }

    #[test]
    fn corrupt_log_is_an_error_not_an_empty_store() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join(LOG_NAME), b"{broken\n").unwrap();
        assert!(FileReplayStore::open(dir.path(), DAY_MS, 1, StateInit::Bootstrap).is_err());
    }

    #[test]
    fn missing_log_in_an_existing_state_dir_is_an_error_not_an_empty_store() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("kill_state.json"), b"{}").unwrap();
        assert!(FileReplayStore::open(dir.path(), DAY_MS, 1, StateInit::Existing).is_err());
        assert!(!dir.path().join(LOG_NAME).exists(), "nothing is created");
    }

    #[test]
    fn bootstrap_creates_the_log_so_the_next_start_finds_it() {
        let dir = tempfile::tempdir().unwrap();
        FileReplayStore::open(dir.path(), DAY_MS, 1, StateInit::Bootstrap).unwrap();
        assert!(dir.path().join(LOG_NAME).exists());
        assert!(FileReplayStore::open(dir.path(), DAY_MS, 2, StateInit::Existing).is_ok());
    }

    #[test]
    fn unavailable_store_fails_every_call() {
        let s = UnavailableReplayStore;
        assert!(s.lookup("a").is_err() && s.record(&rec("a", 1)).is_err());
        assert!(!s.is_healthy());
    }
}
