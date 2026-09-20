//! C01 kill-switch gate and the validity gates C03..C07.

use super::{ReplayVerdict, Snapshot};
use crate::domain::{Mode, Side, ValidatedSignal};
use crate::limits::SessionConfig;
use crate::pb::{ControlResult, KillSwitchLevel, ReasonCode};

const NANOS_PER_MS: i128 = 1_000_000;
const NANOS_PER_MINUTE: i128 = 60_000_000_000;
const NANOS_PER_DAY: i128 = 86_400_000_000_000;

/// True iff filling the order strictly reduces the absolute position in the
/// symbol. Unknown position or any pending order in the symbol => false
/// (treated as risk-increasing).
pub(super) fn is_risk_reducing(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> bool {
    let Some(x) = snap.exposure else {
        return false;
    };
    if x.pending_buy != 0 || x.pending_sell != 0 {
        return false;
    }
    let pos = i128::from(x.position.get());
    let qty = i128::from(sig.qty.get());
    let after = if sig.side.is_sell() {
        pos - qty
    } else {
        pos + qty
    };
    after.abs() < pos.abs()
}

pub(super) fn c01_kill_switch(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    let reason = ReasonCode::ReasonKillSwitchActive;
    let Some(level) = snap.kill else {
        return ControlResult::fail("C01", true, reason, "NORMAL", "UNAVAILABLE")
            .with_detail("kill-switch state unavailable, treated as HARD");
    };
    let observed = level.as_str_name();
    let reducing = is_risk_reducing(sig, snap);
    match level {
        KillSwitchLevel::KillLevelNormal => ControlResult::pass("C01", true, "NORMAL", observed),
        KillSwitchLevel::KillLevelSoft if reducing => {
            ControlResult::pass("C01", true, "NORMAL or risk-reducing", observed)
        }
        // LOGIC: only a human-released risk-reducing order may proceed.
        KillSwitchLevel::KillLevelLogic if reducing && snap.mode == Mode::Release => {
            ControlResult::pass("C01", true, "human-released risk-reducing", observed)
        }
        KillSwitchLevel::KillLevelLogic if reducing => {
            ControlResult::fail("C01", false, reason, "human release", observed)
                .with_detail("risk-reducing order held for human release")
        }
        _ => ControlResult::fail("C01", true, reason, "NORMAL", observed),
    }
}

pub(super) fn c03_symbol_allowlist(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    if snap.limits.symbols.contains_key(&sig.symbol) {
        ControlResult::pass("C03", true, "in universe", sig.symbol.as_str())
    } else {
        ControlResult::fail(
            "C03",
            true,
            ReasonCode::ReasonSymbolNotAllowed,
            "in universe",
            sig.symbol.as_str(),
        )
    }
}

pub(super) fn c04_short_sale(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    if sig.side == Side::SellShort && !snap.limits.short_selling_enabled {
        ControlResult::fail(
            "C04",
            true,
            ReasonCode::ReasonShortSellingDisabled,
            "short selling disabled",
            "SELL_SHORT",
        )
    } else {
        ControlResult::pass("C04", true, "short selling disabled", "ok")
    }
}

/// ISO weekday (1 = Monday) and minute of day (UTC) for an epoch timestamp.
fn weekday_and_minute(now_ns: i64) -> (u8, u16) {
    let now = i128::from(now_ns);
    let days = now.div_euclid(NANOS_PER_DAY);
    let minute = now.rem_euclid(NANOS_PER_DAY) / NANOS_PER_MINUTE;
    // 1970-01-01 was a Thursday (ISO 4).
    let weekday = (days + 3).rem_euclid(7) + 1;
    (
        u8::try_from(weekday).unwrap_or(0),
        u16::try_from(minute).unwrap_or(u16::MAX),
    )
}

/// Is `now_ns` inside the configured UTC session window?
pub fn session_contains(session: &SessionConfig, now_ns: i64) -> bool {
    let (weekday, minute) = weekday_and_minute(now_ns);
    session.weekdays_utc.contains(&weekday)
        && minute >= session.start_minute_utc
        && minute < session.end_minute_utc
}

pub(super) fn c05_session(_sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    if session_contains(&snap.limits.session, snap.now_ns) {
        ControlResult::pass("C05", true, "in session", "in session")
    } else {
        ControlResult::fail(
            "C05",
            true,
            ReasonCode::ReasonOutsideTradingSession,
            "in session",
            "outside session",
        )
    }
}

fn expiry_failure(
    sig: &ValidatedSignal,
    snap: &Snapshot<'_>,
) -> Option<(ReasonCode, &'static str)> {
    let now = i128::from(snap.now_ns);
    let t = &snap.limits.timings;
    let skew = i128::from(t.clock_skew_ms) * NANOS_PER_MS;
    let max_age = i128::from(t.max_signal_age_ms) * NANOS_PER_MS;
    let created = i128::from(sig.created_at_ns);
    let valid_until = i128::from(sig.valid_until_ns);
    if valid_until == 0 {
        return Some((ReasonCode::ReasonSignalExpired, "valid_until_ns unset"));
    }
    if now > valid_until {
        return Some((ReasonCode::ReasonSignalExpired, "past valid_until_ns"));
    }
    if created > now + skew {
        return Some((
            ReasonCode::ReasonSignalFromFuture,
            "created_at_ns in the future",
        ));
    }
    if now - created > max_age {
        return Some((ReasonCode::ReasonSignalExpired, "older than max_signal_age"));
    }
    None
}

pub(super) fn c06_expiry(sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    match expiry_failure(sig, snap) {
        None => ControlResult::pass("C06", true, "fresh", "fresh"),
        Some((reason, why)) => {
            ControlResult::fail("C06", true, reason, "fresh", why).with_detail(why)
        }
    }
}

pub(super) fn c07_replay(_sig: &ValidatedSignal, snap: &Snapshot<'_>) -> ControlResult {
    if snap.mode == Mode::Release {
        return ControlResult::pass("C07", true, "held signal", "release");
    }
    let (reason, observed) = match snap.replay {
        ReplayVerdict::New => return ControlResult::pass("C07", true, "new signal_id", "new"),
        ReplayVerdict::Duplicate => (ReasonCode::ReasonDuplicateSignal, "duplicate"),
        ReplayVerdict::PayloadMismatch => {
            (ReasonCode::ReasonReplayPayloadMismatch, "payload mismatch")
        }
        ReplayVerdict::StoreUnavailable => {
            (ReasonCode::ReasonStateUnavailable, "store unavailable")
        }
    };
    ControlResult::fail("C07", true, reason, "new signal_id", observed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn weekday_and_minute_known_instants() {
        // 1970-01-01T00:00Z Thursday
        assert_eq!(weekday_and_minute(0), (4, 0));
        // 2026-09-21T14:30:00Z is a Monday (1_790_000_000 is 2026-09-21T14:13:20Z)
        let ns = 1_790_000_000_i64 * 1_000_000_000;
        assert_eq!(weekday_and_minute(ns), (1, 14 * 60 + 13));
        // pre-epoch instants stay in range
        assert_eq!(weekday_and_minute(-1), (3, 1_439));
    }
}
