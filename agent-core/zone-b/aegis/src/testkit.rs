//! Test support shared by unit and integration tests (feature `testkit`,
//! enabled for the crate's own tests through a dev-dependency on itself; it is
//! NOT compiled into the production binary).
//!
//! `Rig::default()` builds an Aegis whose every dependency is in memory and
//! whose baseline signal is APPROVED: fresh market data, a matching regime, a
//! seeded order-size history (so C19 does not hold) and a day-start equity.

use std::sync::Arc;

use crate::audit::{AuditSink, MemorySink};
use crate::clock::{Clock, ManualClock};
use crate::config::Environment;
use crate::controls::{RefPrice, RegimeView};
use crate::engine::{Engine, EngineDeps};
use crate::killswitch::controller::KillController;
use crate::killswitch::store::MemoryKillStore;
use crate::killswitch::HeartbeatConfig;
use crate::limits::Limits;
use crate::money::Nanos;
use crate::pb;
use crate::signing::dev::DevEd25519Signer;
use crate::signing::verify::Ed25519Verifier;
use crate::signing::Signer;
use crate::state::portfolio::{MemoryPortfolioStore, Portfolio, PortfolioStore};
use crate::state::refdata::MemoryReferenceData;
use crate::state::replay::{MemoryReplayStore, ReplayStore};

/// 2026-09-21T14:13:20Z, a Monday inside the test session (13:30-20:00 UTC).
pub const NOW_NS: i64 = 1_790_000_000 * 1_000_000_000;
pub const SHARE: i64 = 1_000_000_000;

pub fn limits_json() -> String {
    r#"{
      "symbols": {"AAPL": {"max_order_qty_nanos": 100000000000, "max_order_notional_nanos": 50000000000000, "max_position_nanos": 500000000000},
                  "MSFT": {"max_order_qty_nanos": 50000000000, "max_order_notional_nanos": 50000000000000, "max_position_nanos": 200000000000}},
      "short_selling_enabled": false,
      "accept_legacy_double_fields": false,
      "session": {"weekdays_utc": [1,2,3,4,5], "start_minute_utc": 810, "end_minute_utc": 1200},
      "price_collar_bps": 150,
      "max_order_adv_bps": 50,
      "max_gross_exposure_nanos": 1000000000000000,
      "max_daily_drawdown_bps": 200,
      "rate_global": {"capacity": 20, "refill_per_sec": 10},
      "rate_per_symbol": {"capacity": 5, "refill_per_sec": 2},
      "omega_min": 0.55,
      "regime": {"min_confidence": 0.6, "allowed": ["TRENDING_BULL", "LOW_VOL_CHOP"]},
      "hold_requires_second_approver": false,
      "hold_requires_cooling_period": false,
      "hold_max_distress_score": 0.8,
      "audit_mandatory": true
    }"#
    .to_owned()
}

/// `00000000-0000-4000-8000-<n:012>`: a valid canonical UUID per `n`.
pub fn uuid_n(n: u64) -> String {
    format!("00000000-0000-4000-8000-{n:012}")
}

#[derive(Default)]
pub struct RigOptions {
    pub limits_json: Option<String>,
    pub audit: Option<Arc<dyn AuditSink>>,
    pub signer: Option<Arc<dyn Signer>>,
    pub replay: Option<Arc<dyn ReplayStore>>,
    pub portfolio_store: Option<Arc<MemoryPortfolioStore>>,
    pub kill_store: Option<Arc<MemoryKillStore>>,
    /// Skip seeding order-size history (cold start: C19 holds everything).
    pub cold_start: bool,
    /// Skip the day-start equity baseline (C15 fails closed).
    pub no_equity: bool,
    /// Skip seeding reference data (C08 fails until something is pushed).
    pub no_market: bool,
}

pub struct Rig {
    pub engine: Arc<Engine>,
    pub clock: ManualClock,
    pub kill: Arc<KillController>,
    pub kill_store: Arc<MemoryKillStore>,
    pub audit: Arc<MemorySink>,
    pub refdata: Arc<MemoryReferenceData>,
    pub portfolio_store: Arc<MemoryPortfolioStore>,
    pub signer: Arc<DevEd25519Signer>,
    pub limits: Arc<Limits>,
}

impl Default for Rig {
    fn default() -> Rig {
        Rig::build(RigOptions::default())
    }
}

fn seeded_portfolio(o: &RigOptions) -> Portfolio {
    let mut p = Portfolio::default();
    if !o.cold_start {
        for i in 0..30 {
            p.record_size(
                "AAPL",
                Nanos::new(100 * SHARE),
                NOW_NS - 3_600_000_000_000 + i,
            );
        }
    }
    if !o.no_equity {
        p.record_equity(1_000_000 * SHARE, NOW_NS - 1_000_000_000);
    }
    p
}

impl Rig {
    pub fn build(o: RigOptions) -> Rig {
        let limits = Arc::new(
            Limits::from_bytes(o.limits_json.clone().unwrap_or_else(limits_json).as_bytes())
                .expect("testkit limits"),
        );
        let clock = ManualClock::new(NOW_NS);
        let audit = Arc::new(MemorySink::default());
        let audit_dyn: Arc<dyn AuditSink> = o.audit.clone().unwrap_or_else(|| audit.clone());
        let kill_store = o.kill_store.clone().unwrap_or_default();
        let t = &limits.config.timings;
        let kill = Arc::new(KillController::start(
            kill_store.clone(),
            audit_dyn.clone(),
            Arc::new(clock.clone()),
            HeartbeatConfig::from_ms(
                t.operator_heartbeat_interval_ms,
                t.operator_heartbeat_warn_ms,
            ),
            limits.config.audit_mandatory,
        ));
        let portfolio_store = o.portfolio_store.clone().unwrap_or_default();
        if portfolio_store.saved().is_none() {
            portfolio_store
                .save(&seeded_portfolio(&o))
                .expect("seed portfolio");
        }
        let signer =
            Arc::new(DevEd25519Signer::from_seed(Environment::Test, [42; 32]).expect("dev signer"));
        let refdata = Arc::new(MemoryReferenceData::default());
        let replay: Arc<dyn ReplayStore> = o
            .replay
            .clone()
            .unwrap_or_else(|| Arc::new(MemoryReplayStore::new(t.replay_retention_ms)));
        let signer_dyn: Arc<dyn Signer> = o.signer.clone().unwrap_or_else(|| signer.clone());
        let engine = Arc::new(Engine::new(EngineDeps {
            limits: limits.clone(),
            clock: Arc::new(clock.clone()) as Arc<dyn Clock>,
            kill: kill.clone(),
            signer: signer_dyn,
            audit: audit_dyn,
            replay,
            portfolio_store: portfolio_store.clone(),
            refdata: refdata.clone(),
        }));
        let rig = Rig {
            engine,
            clock,
            kill,
            kill_store,
            audit,
            refdata,
            portfolio_store,
            signer,
            limits,
        };
        if !o.no_market {
            rig.refresh_market();
        }
        rig
    }

    pub fn now_ns(&self) -> i64 {
        self.clock.now_ns().unwrap_or(NOW_NS)
    }

    /// Re-stamp market data and regime at the current clock time.
    pub fn refresh_market(&self) {
        let now = self.clock.now_ns().unwrap_or(NOW_NS);
        let px = RefPrice {
            mid: Nanos::new(150 * SHARE),
            adv: Nanos::new(1_000_000 * SHARE),
            ingested_at_ns: now,
            is_stale: false,
        };
        self.refdata.set_price("AAPL", px).expect("set price");
        self.refdata
            .set_regime(RegimeView {
                label: pb::RegimeLabel::TrendingBull,
                confidence: 0.9,
                at_ns: now,
            })
            .expect("set regime");
    }

    /// A signal that the baseline rig approves: BUY `shares` AAPL at 150.
    pub fn signal(&self, n: u64, shares: i64) -> pb::TradeSignal {
        let now = self.clock.now_ns().unwrap_or(NOW_NS);
        pb::TradeSignal {
            signal_id: uuid_n(n),
            symbol: "AAPL".into(),
            created_at_ns: now - 100_000_000,
            side: pb::SignalSide::Buy as i32,
            omega: 0.8,
            regime: pb::RegimeLabel::TrendingBull as i32,
            regime_confidence: 0.9,
            valid_until_ns: now + 4_000_000_000,
            quantity_nanos: shares * SHARE,
            price_limit_nanos: 150 * SHARE,
            strategy_id: "AFE-STRATEGY-001".into(),
            ..pb::TradeSignal::default()
        }
    }

    /// Public key registry for verifying this rig's attestations.
    pub fn verifier(&self) -> Ed25519Verifier {
        Ed25519Verifier::default().with_key(self.signer.key_id(), self.signer.verifying_key())
    }
}
