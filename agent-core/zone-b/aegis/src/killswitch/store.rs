//! Durable kill-switch state (spec 5.1: latches are sticky and survive
//! restarts; unreadable state => HARD).
//!
//! The file store writes to a temp file, fsyncs, renames over the target and
//! fsyncs the directory, so a crash leaves either the old or the new state.
//! A MISSING state file in a NON-EMPTY state directory is treated as tampering
//! or loss (unreadable => HARD), not as first boot.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use super::{KillState, MAX_LATCHES};

const FORMAT_VERSION: u32 = 1;
const STATE_FILE: &str = "kill_state.json";
const TMP_FILE: &str = "kill_state.json.tmp";

#[derive(Debug, Error)]
pub enum StoreError {
    #[error("kill-switch state store write failed: {0}")]
    Write(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LoadOutcome {
    /// Nothing persisted and nothing else in the state dir: first boot.
    Fresh,
    Loaded(KillState),
    /// Present but unusable, or absent where it must exist: start at HARD.
    Unreadable(String),
}

pub trait KillStore: Send + Sync {
    fn load(&self) -> LoadOutcome;
    fn save(&self, state: &KillState) -> Result<(), StoreError>;
    /// Called after `Unreadable` so the bad artefact is preserved for forensics.
    fn quarantine(&self) {}
}

#[derive(Serialize, Deserialize)]
struct OnDisk {
    version: u32,
    state: KillState,
}

fn decode(bytes: &[u8]) -> LoadOutcome {
    match serde_json::from_slice::<OnDisk>(bytes) {
        Ok(d) if d.version == FORMAT_VERSION && d.state.latches.len() <= MAX_LATCHES => {
            LoadOutcome::Loaded(d.state)
        }
        Ok(d) => LoadOutcome::Unreadable(format!(
            "unsupported version {} or too many latches",
            d.version
        )),
        Err(e) => LoadOutcome::Unreadable(format!("parse error: {e}")),
    }
}

/// File-backed store under `AEGIS_STATE_DIR`.
#[derive(Debug)]
pub struct FileKillStore {
    dir: PathBuf,
}

impl FileKillStore {
    pub fn new(dir: &Path) -> FileKillStore {
        FileKillStore {
            dir: dir.to_path_buf(),
        }
    }

    fn path(&self) -> PathBuf {
        self.dir.join(STATE_FILE)
    }

    fn dir_has_other_entries(&self) -> Result<bool, std::io::Error> {
        for entry in fs::read_dir(&self.dir)? {
            let name = entry?.file_name();
            if name != STATE_FILE && name != TMP_FILE {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

impl KillStore for FileKillStore {
    fn load(&self) -> LoadOutcome {
        match fs::read(self.path()) {
            Ok(bytes) => decode(&bytes),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                match self.dir_has_other_entries() {
                    Ok(false) => LoadOutcome::Fresh,
                    Ok(true) => LoadOutcome::Unreadable(
                        "state file missing in a non-empty state dir".into(),
                    ),
                    Err(e) => LoadOutcome::Unreadable(format!("state dir unreadable: {e}")),
                }
            }
            Err(e) => LoadOutcome::Unreadable(format!("state file unreadable: {e}")),
        }
    }

    fn save(&self, state: &KillState) -> Result<(), StoreError> {
        let err = |what: &str, e: std::io::Error| StoreError::Write(format!("{what}: {e}"));
        let body = serde_json::to_vec(&OnDisk {
            version: FORMAT_VERSION,
            state: state.clone(),
        })
        .map_err(|e| StoreError::Write(format!("serialise: {e}")))?;
        let tmp = self.dir.join(TMP_FILE);
        let mut f = fs::File::create(&tmp).map_err(|e| err("create tmp", e))?;
        f.write_all(&body).map_err(|e| err("write", e))?;
        f.sync_all().map_err(|e| err("fsync", e))?;
        drop(f);
        fs::rename(&tmp, self.path()).map_err(|e| err("rename", e))?;
        #[cfg(unix)]
        fs::File::open(&self.dir)
            .and_then(|d| d.sync_all())
            .map_err(|e| err("fsync dir", e))?;
        Ok(())
    }

    fn quarantine(&self) {
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        if self.path().exists() {
            let _ = fs::rename(
                self.path(),
                self.dir.join(format!("{STATE_FILE}.corrupt-{ts}")),
            );
        }
    }
}

/// In-memory store for tests and embedding; can simulate write failure and an
/// unreadable state.
#[derive(Debug, Default)]
pub struct MemoryKillStore {
    state: Mutex<Option<KillState>>,
    unreadable: AtomicBool,
    fail_saves: AtomicBool,
}

impl MemoryKillStore {
    pub fn with_state(state: KillState) -> MemoryKillStore {
        MemoryKillStore {
            state: Mutex::new(Some(state)),
            ..MemoryKillStore::default()
        }
    }

    pub fn set_unreadable(&self, v: bool) {
        self.unreadable.store(v, Ordering::SeqCst);
    }

    pub fn set_fail_saves(&self, v: bool) {
        self.fail_saves.store(v, Ordering::SeqCst);
    }

    pub fn saved(&self) -> Option<KillState> {
        self.state.lock().ok().and_then(|s| s.clone())
    }
}

impl KillStore for MemoryKillStore {
    fn load(&self) -> LoadOutcome {
        if self.unreadable.load(Ordering::SeqCst) {
            return LoadOutcome::Unreadable("simulated".into());
        }
        match self.state.lock() {
            Ok(s) => s.clone().map_or(LoadOutcome::Fresh, LoadOutcome::Loaded),
            Err(_) => LoadOutcome::Unreadable("poisoned".into()),
        }
    }

    fn save(&self, state: &KillState) -> Result<(), StoreError> {
        if self.fail_saves.load(Ordering::SeqCst) {
            return Err(StoreError::Write("simulated failure".into()));
        }
        *self
            .state
            .lock()
            .map_err(|_| StoreError::Write("poisoned".into()))? = Some(state.clone());
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::killswitch::Latch;

    fn sample() -> KillState {
        KillState {
            latches: vec![Latch {
                trigger_id: "t1".into(),
                level: 4,
                reason: "r".into(),
                actor_id: "a".into(),
                latched_at_ns: 7,
            }],
            state_seq: 3,
            updated_at_ns: 9,
            last_operator_heartbeat_ns: 5,
        }
    }

    #[test]
    fn empty_dir_is_fresh_and_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileKillStore::new(dir.path());
        assert_eq!(store.load(), LoadOutcome::Fresh);
        store.save(&sample()).unwrap();
        assert_eq!(store.load(), LoadOutcome::Loaded(sample()));
    }

    #[test]
    fn missing_state_file_in_non_empty_dir_is_unreadable_not_fresh() {
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("audit.wal"), b"x").unwrap();
        assert!(matches!(
            FileKillStore::new(dir.path()).load(),
            LoadOutcome::Unreadable(_)
        ));
    }

    #[test]
    fn corrupt_or_wrong_version_state_is_unreadable_and_quarantined() {
        let dir = tempfile::tempdir().unwrap();
        let store = FileKillStore::new(dir.path());
        fs::write(dir.path().join(STATE_FILE), b"{not json").unwrap();
        assert!(matches!(store.load(), LoadOutcome::Unreadable(_)));
        fs::write(
            dir.path().join(STATE_FILE),
            br#"{"version":99,"state":{"latches":[],"state_seq":1,"updated_at_ns":1,"last_operator_heartbeat_ns":1}}"#,
        )
        .unwrap();
        assert!(matches!(store.load(), LoadOutcome::Unreadable(_)));
        store.quarantine();
        assert!(!dir.path().join(STATE_FILE).exists());
        assert!(fs::read_dir(dir.path()).unwrap().any(|e| e
            .unwrap()
            .file_name()
            .to_string_lossy()
            .contains("corrupt")));
    }

    #[test]
    fn memory_store_simulates_failures() {
        let m = MemoryKillStore::default();
        assert_eq!(m.load(), LoadOutcome::Fresh);
        m.set_fail_saves(true);
        assert!(m.save(&sample()).is_err());
        m.set_fail_saves(false);
        m.save(&sample()).unwrap();
        m.set_unreadable(true);
        assert!(matches!(m.load(), LoadOutcome::Unreadable(_)));
    }
}
