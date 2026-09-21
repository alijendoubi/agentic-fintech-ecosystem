//! `ReportExecution`: fills and account equity from the execution-motor, the
//! source of truth for positions, drawdown and NAV (spec section 6).
//!
//! Reports carry CUMULATIVE filled quantity, so duplicates are idempotent. An
//! equity-only report has an empty `order_id`. The first equity report of a UTC
//! day sets that day's starting NAV (until then C15 fails closed). A breach of
//! the daily drawdown latches LOGIC (spec C15 / 5.2).

use super::Engine;
use crate::money::bps_of_down;
use crate::pb::{Ack, ExecutionReport, KillSwitchLevel, OrderSide, OrderStatus};
use crate::state::portfolio::FillReport;

const DRAWDOWN_ACTOR: &str = "aegis/drawdown";

fn ack(ok: bool, detail: &str) -> Ack {
    Ack {
        ok,
        detail: detail.to_owned(),
    }
}

impl Engine {
    pub fn report_execution(&self, r: &ExecutionReport) -> Ack {
        let Some(now) = self.now() else {
            return ack(false, "clock unavailable");
        };
        if r.account_equity_nanos < 0
            || r.filled_qty_nanos < 0
            || OrderStatus::try_from(r.status).is_err()
        {
            return ack(false, "invalid report");
        }
        let Some(mut core) = self.lock() else {
            return ack(false, "engine state unavailable");
        };
        let Some(current) = core.portfolio.as_ref() else {
            return ack(false, "portfolio state unavailable");
        };
        let mut candidate = current.clone();
        if !r.order_id.is_empty() {
            let is_sell = match OrderSide::try_from(r.side) {
                Ok(OrderSide::OrderBuy) => false,
                Ok(OrderSide::OrderSell) => true,
                _ => return ack(false, "unknown order side"),
            };
            let fill = FillReport {
                order_id: &r.order_id,
                symbol: &r.symbol,
                is_sell,
                status: r.status,
                cum_filled: r.filled_qty_nanos,
                now_ns: now,
            };
            if let Err(e) = candidate.apply_fill_report(&fill) {
                tracing::warn!(error = %e, order_id = %r.order_id, "execution report refused");
                return ack(false, "report refused");
            }
        }
        if r.account_equity_nanos > 0 {
            candidate.record_equity(r.account_equity_nanos, now);
        }
        candidate.expire_reservations(now);
        if self.deps.portfolio_store.save(&candidate).is_err() {
            return ack(false, "state store unavailable");
        }
        let breach = self.drawdown_breached(&candidate, now);
        core.portfolio = Some(candidate);
        drop(core);
        if breach {
            self.latch_drawdown();
        }
        ack(true, "")
    }

    fn drawdown_breached(&self, p: &crate::state::portfolio::Portfolio, now: i64) -> bool {
        let Some((start, current)) = p.equity_today(now) else {
            return false;
        };
        let start = i128::from(start.get());
        let bps = self.deps.limits.config.max_daily_drawdown_bps;
        match bps_of_down(start, bps) {
            Ok(limit) => start > 0 && (limit == 0 || start - i128::from(current.get()) >= limit),
            Err(_) => true,
        }
    }

    fn latch_drawdown(&self) {
        let already = self
            .deps
            .kill
            .state()
            .latches
            .iter()
            .any(|l| l.actor_id == DRAWDOWN_ACTOR);
        if already {
            return;
        }
        if let Err(e) = self.deps.kill.trigger(
            KillSwitchLevel::KillLevelLogic as i32,
            "daily drawdown limit reached",
            DRAWDOWN_ACTOR,
        ) {
            tracing::error!(error = %e, "drawdown latch not persisted (state forced to HARD)");
        }
    }
}
