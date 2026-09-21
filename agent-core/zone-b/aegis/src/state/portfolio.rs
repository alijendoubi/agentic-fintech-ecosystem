//! Positions, pending (approved but unfilled) orders, day equity and order-size
//! history. The source of truth is `ReportExecution` (spec section 6).
//!
//! Exposure checks are WORST CASE: every approved-but-unfilled order counts as
//! if it fills in the worst direction, until the execution-motor reports it
//! terminal. An approved order that is never reported is released only after
//! its attestation expired (it can no longer be submitted). Fill reports carry
//! cumulative quantities, so a duplicate or replayed report is idempotent.

use std::collections::BTreeMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use serde::{Deserialize, Serialize};

use super::{sync_dir, StateError, StateInit};
use crate::controls::{ExposureView, SizeHistory};
use crate::money::{mul_up, Nanos};

const NANOS_PER_DAY: i64 = 86_400_000_000_000;
/// Bound on retained order-size samples per symbol.
const MAX_SIZE_SAMPLES: usize = 2_000;
/// Bound on tracked open orders.
pub const MAX_OPEN_ORDERS: usize = 10_000;
const STATE_FILE: &str = "portfolio.json";
const FORMAT_VERSION: u32 = 1;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OpenOrder {
    pub symbol: String,
    pub is_sell: bool,
    pub qty: i64,
    pub cum_filled: i64,
    /// Attestation expiry: an unreported order is released after this.
    pub expires_at_ns: i64,
    /// True once the execution-motor reported it (it was really submitted).
    pub submitted: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ClosedOrder {
    pub symbol: String,
    pub is_sell: bool,
    pub cum_filled: i64,
    pub closed_at_ns: i64,
}

/// How long terminal orders are remembered for duplicate-report idempotency.
const CLOSED_RETENTION_NS: i64 = 7 * NANOS_PER_DAY;
/// Bound on remembered terminal orders.
const MAX_CLOSED_ORDERS: usize = 50_000;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DayEquity {
    pub day: i64,
    pub start: i64,
    pub current: i64,
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Portfolio {
    pub positions: BTreeMap<String, i64>,
    pub open_orders: BTreeMap<String, OpenOrder>,
    /// Terminal orders, remembered so a duplicate/late report is idempotent.
    #[serde(default)]
    pub closed_orders: BTreeMap<String, ClosedOrder>,
    pub day: Option<DayEquity>,
    /// (recorded_at_ns, qty_nanos) per symbol, oldest first.
    pub size_samples: BTreeMap<String, Vec<(i64, i64)>>,
}

/// One execution report's fill data.
#[derive(Debug, Clone, Copy)]
pub struct FillReport<'a> {
    pub order_id: &'a str,
    pub symbol: &'a str,
    pub is_sell: bool,
    pub status: i32,
    pub cum_filled: i64,
    pub now_ns: i64,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ReportError {
    #[error("negative filled quantity")]
    NegativeFill,
    #[error("cumulative filled quantity decreased")]
    DecreasingFill,
    #[error("report contradicts the recorded symbol or side of the order")]
    Mismatch,
    #[error("position overflow")]
    Overflow,
}

/// Terminal statuses free the reservation.
pub fn is_terminal(status: i32) -> bool {
    use crate::pb::OrderStatus as S;
    matches!(
        S::try_from(status),
        Ok(S::OrderFilled | S::OrderCancelled | S::OrderRejected)
    )
}

impl Portfolio {
    pub fn day_index(now_ns: i64) -> i64 {
        now_ns.div_euclid(NANOS_PER_DAY)
    }

    /// Reserve exposure for an approved order.
    pub fn reserve(
        &mut self,
        order_id: &str,
        symbol: &str,
        is_sell: bool,
        qty: Nanos,
        expires_at_ns: i64,
    ) -> Result<(), StateError> {
        if self.open_orders.len() >= MAX_OPEN_ORDERS {
            return Err(StateError::Unavailable("too many open orders".into()));
        }
        self.open_orders.insert(
            order_id.to_owned(),
            OpenOrder {
                symbol: symbol.to_owned(),
                is_sell,
                qty: qty.get(),
                cum_filled: 0,
                expires_at_ns,
                submitted: false,
            },
        );
        Ok(())
    }

    /// Drop a reservation whose order was never reported as submitted (its
    /// attestation was abandoned). Returns whether one was removed.
    pub fn release_unsubmitted(&mut self, order_id: &str) -> bool {
        match self.open_orders.get(order_id) {
            Some(o) if !o.submitted => self.open_orders.remove(order_id).is_some(),
            _ => false,
        }
    }

    /// Apply an execution report (cumulative fill quantity). Idempotent for
    /// duplicates; refuses a decreasing cumulative quantity or a report that
    /// contradicts the order's recorded symbol/side.
    pub fn apply_fill_report(&mut self, r: &FillReport<'_>) -> Result<(), ReportError> {
        if r.cum_filled < 0 {
            return Err(ReportError::NegativeFill);
        }
        if self.closed_orders.contains_key(r.order_id) {
            return self.apply_to_closed(r);
        }
        let order = self
            .open_orders
            .entry(r.order_id.to_owned())
            .or_insert_with(|| OpenOrder {
                // Not one of ours (or already released): track it so fills
                // still move the position, with nothing left pending.
                symbol: r.symbol.to_owned(),
                is_sell: r.is_sell,
                qty: r.cum_filled,
                cum_filled: 0,
                expires_at_ns: i64::MAX,
                submitted: true,
            });
        if order.symbol != r.symbol || order.is_sell != r.is_sell {
            return Err(ReportError::Mismatch);
        }
        if r.cum_filled < order.cum_filled {
            return Err(ReportError::DecreasingFill);
        }
        let delta = r.cum_filled - order.cum_filled;
        let signed = if r.is_sell { -delta } else { delta };
        let pos = self
            .positions
            .get(r.symbol)
            .copied()
            .unwrap_or(0)
            .checked_add(signed)
            .ok_or(ReportError::Overflow)?;
        order.cum_filled = r.cum_filled;
        order.qty = order.qty.max(r.cum_filled);
        order.submitted = true;
        self.positions.insert(r.symbol.to_owned(), pos);
        if is_terminal(r.status) {
            self.open_orders.remove(r.order_id);
            if self.closed_orders.len() < MAX_CLOSED_ORDERS {
                self.closed_orders.insert(
                    r.order_id.to_owned(),
                    ClosedOrder {
                        symbol: r.symbol.to_owned(),
                        is_sell: r.is_sell,
                        cum_filled: r.cum_filled,
                        closed_at_ns: r.now_ns,
                    },
                );
            }
        }
        Ok(())
    }

    /// A late or duplicate report for an already terminal order: only a larger
    /// cumulative quantity moves the position.
    fn apply_to_closed(&mut self, r: &FillReport<'_>) -> Result<(), ReportError> {
        let Some(c) = self.closed_orders.get_mut(r.order_id) else {
            return Err(ReportError::Mismatch);
        };
        if c.symbol != r.symbol || c.is_sell != r.is_sell {
            return Err(ReportError::Mismatch);
        }
        if r.cum_filled < c.cum_filled {
            return Err(ReportError::DecreasingFill);
        }
        let delta = r.cum_filled - c.cum_filled;
        let signed = if r.is_sell { -delta } else { delta };
        let pos = self
            .positions
            .get(r.symbol)
            .copied()
            .unwrap_or(0)
            .checked_add(signed)
            .ok_or(ReportError::Overflow)?;
        c.cum_filled = r.cum_filled;
        self.positions.insert(r.symbol.to_owned(), pos);
        Ok(())
    }

    /// Drop reservations that were never reported and whose attestation expired.
    pub fn expire_reservations(&mut self, now_ns: i64) {
        self.open_orders
            .retain(|_, o| o.submitted || o.expires_at_ns >= now_ns);
        self.closed_orders
            .retain(|_, c| c.closed_at_ns.saturating_add(CLOSED_RETENTION_NS) >= now_ns);
    }

    fn pending(&self, symbol: &str) -> (i128, i128) {
        let (mut buy, mut sell) = (0i128, 0i128);
        for o in self.open_orders.values().filter(|o| o.symbol == symbol) {
            let remaining = i128::from(o.qty) - i128::from(o.cum_filled);
            if o.is_sell {
                sell += remaining.max(0);
            } else {
                buy += remaining.max(0);
            }
        }
        (buy, sell)
    }

    fn position(&self, symbol: &str) -> i64 {
        self.positions.get(symbol).copied().unwrap_or(0)
    }

    /// Worst-case gross exposure of every symbol EXCEPT `symbol`, valued at
    /// `mark(sym)`. `None` if a needed mark is missing or on overflow.
    fn others_gross(&self, symbol: &str, mark: &dyn Fn(&str) -> Option<Nanos>) -> Option<i128> {
        let mut symbols: Vec<&str> = self.positions.keys().map(String::as_str).collect();
        symbols.extend(self.open_orders.values().map(|o| o.symbol.as_str()));
        symbols.sort_unstable();
        symbols.dedup();
        let mut total = 0i128;
        for s in symbols.into_iter().filter(|s| *s != symbol) {
            let pos = i128::from(self.position(s));
            let (pb, ps) = self.pending(s);
            let worst = (pos + pb).abs().max((pos - ps).abs());
            if worst == 0 {
                continue;
            }
            let px = mark(s)?;
            total = total.checked_add(mul_up(worst, i128::from(px.get())).ok()?)?;
        }
        Some(total)
    }

    pub fn exposure_view(
        &self,
        symbol: &str,
        mark: &dyn Fn(&str) -> Option<Nanos>,
    ) -> ExposureView {
        let (pending_buy, pending_sell) = self.pending(symbol);
        ExposureView {
            position: Nanos::new(self.position(symbol)),
            pending_buy,
            pending_sell,
            others_gross: self.others_gross(symbol, mark),
        }
    }

    /// Equity for the current UTC day, or `None` until the first equity report
    /// of the day (fail closed).
    pub fn equity_today(&self, now_ns: i64) -> Option<(Nanos, Nanos)> {
        self.day
            .as_ref()
            .filter(|d| d.day == Portfolio::day_index(now_ns))
            .map(|d| (Nanos::new(d.start), Nanos::new(d.current)))
    }

    pub fn record_equity(&mut self, equity: i64, now_ns: i64) {
        let today = Portfolio::day_index(now_ns);
        match self.day.as_mut() {
            Some(d) if d.day == today => d.current = equity,
            _ => {
                self.day = Some(DayEquity {
                    day: today,
                    start: equity,
                    current: equity,
                })
            }
        }
    }

    /// Record an approved order's size for C19 (bounded, oldest dropped).
    pub fn record_size(&mut self, symbol: &str, qty: Nanos, now_ns: i64) {
        let v = self.size_samples.entry(symbol.to_owned()).or_default();
        v.push((now_ns, qty.get()));
        if v.len() > MAX_SIZE_SAMPLES {
            v.remove(0);
        }
    }

    /// Trailing distribution for C19: count and nearest-rank P95.
    pub fn size_history(&self, symbol: &str, now_ns: i64, window_days: u32) -> SizeHistory {
        let window = i64::from(window_days).saturating_mul(NANOS_PER_DAY);
        let cutoff = now_ns.saturating_sub(window);
        let mut qtys: Vec<i64> = self
            .size_samples
            .get(symbol)
            .map(|v| {
                v.iter()
                    .filter(|(t, _)| *t >= cutoff)
                    .map(|(_, q)| *q)
                    .collect()
            })
            .unwrap_or_default();
        qtys.sort_unstable();
        let n = qtys.len();
        let p95 = if n == 0 {
            None
        } else {
            // nearest rank: ceil(0.95 * n) - 1
            qtys.get((95 * n).div_ceil(100) - 1)
                .copied()
                .map(Nanos::new)
        };
        SizeHistory { count: n, p95 }
    }
}

pub trait PortfolioStore: Send + Sync {
    fn load(&self) -> Result<Portfolio, StateError>;
    fn save(&self, p: &Portfolio) -> Result<(), StateError>;
}

#[derive(Serialize, Deserialize)]
struct OnDisk {
    version: u32,
    portfolio: Portfolio,
}

/// Atomic JSON snapshot in the state dir. A missing file is an empty portfolio
/// only on an explicit one-shot [`StateInit::Bootstrap`] (first boot of an
/// empty state dir; the empty snapshot is persisted immediately so the next
/// start finds it). Otherwise a missing file is loss or tampering and a corrupt
/// file is likewise an ERROR (the engine then has no exposure data and rejects
/// everything): assuming a flat portfolio would fail open on exposure limits.
#[derive(Debug)]
pub struct FilePortfolioStore {
    dir: PathBuf,
    bootstrap_pending: AtomicBool,
}

impl FilePortfolioStore {
    pub fn new(dir: &Path, init: StateInit) -> FilePortfolioStore {
        FilePortfolioStore {
            dir: dir.to_path_buf(),
            bootstrap_pending: AtomicBool::new(init == StateInit::Bootstrap),
        }
    }
}

impl PortfolioStore for FilePortfolioStore {
    fn load(&self) -> Result<Portfolio, StateError> {
        let path = self.dir.join(STATE_FILE);
        match fs::read(&path) {
            Ok(bytes) => {
                let d: OnDisk = serde_json::from_slice(&bytes)
                    .map_err(|e| StateError::Unavailable(format!("corrupt portfolio: {e}")))?;
                if d.version != FORMAT_VERSION {
                    return Err(StateError::Unavailable("portfolio version".into()));
                }
                Ok(d.portfolio)
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                if self.bootstrap_pending.swap(false, Ordering::SeqCst) {
                    let flat = Portfolio::default();
                    self.save(&flat)?;
                    Ok(flat)
                } else {
                    Err(StateError::Unavailable(
                        "portfolio.json is missing from an existing state dir: refusing to \
                         assume a flat portfolio"
                            .into(),
                    ))
                }
            }
            Err(e) => Err(StateError::Unavailable(format!("read portfolio: {e}"))),
        }
    }

    fn save(&self, p: &Portfolio) -> Result<(), StateError> {
        let err = |what: &str, e: std::io::Error| StateError::Unavailable(format!("{what}: {e}"));
        let body = serde_json::to_vec(&OnDisk {
            version: FORMAT_VERSION,
            portfolio: p.clone(),
        })
        .map_err(|e| StateError::Unavailable(e.to_string()))?;
        let tmp = self.dir.join("portfolio.json.tmp");
        let mut f = fs::File::create(&tmp).map_err(|e| err("create", e))?;
        f.write_all(&body).map_err(|e| err("write", e))?;
        f.sync_all().map_err(|e| err("fsync", e))?;
        fs::rename(&tmp, self.dir.join(STATE_FILE)).map_err(|e| err("rename", e))?;
        sync_dir(&self.dir).map_err(|e| err("fsync dir", e))
    }
}

/// In-memory store (tests / embedding); can simulate failing writes.
#[derive(Debug, Default)]
pub struct MemoryPortfolioStore {
    state: std::sync::Mutex<Option<Portfolio>>,
    fail_saves: std::sync::atomic::AtomicBool,
    after_save: std::sync::Mutex<Option<SaveHook>>,
}

/// Test hook run after every successful save (e.g. to simulate a caller
/// deadline expiring during a slow fsync).
#[derive(Clone)]
struct SaveHook(std::sync::Arc<dyn Fn() + Send + Sync>);

impl std::fmt::Debug for SaveHook {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("SaveHook")
    }
}

impl MemoryPortfolioStore {
    /// Run `hook` after each successful save.
    pub fn set_after_save(&self, hook: std::sync::Arc<dyn Fn() + Send + Sync>) {
        if let Ok(mut h) = self.after_save.lock() {
            *h = Some(SaveHook(hook));
        }
    }

    pub fn set_fail_saves(&self, v: bool) {
        self.fail_saves
            .store(v, std::sync::atomic::Ordering::SeqCst);
    }

    pub fn saved(&self) -> Option<Portfolio> {
        self.state.lock().ok().and_then(|s| s.clone())
    }
}

impl PortfolioStore for MemoryPortfolioStore {
    fn load(&self) -> Result<Portfolio, StateError> {
        Ok(self
            .state
            .lock()
            .map_err(|_| StateError::Unavailable("poisoned".into()))?
            .clone()
            .unwrap_or_default())
    }

    fn save(&self, p: &Portfolio) -> Result<(), StateError> {
        if self.fail_saves.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(StateError::Unavailable("simulated save failure".into()));
        }
        *self
            .state
            .lock()
            .map_err(|_| StateError::Unavailable("poisoned".into()))? = Some(p.clone());
        let hook = self.after_save.lock().ok().and_then(|h| h.clone());
        if let Some(SaveHook(f)) = hook {
            f();
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
