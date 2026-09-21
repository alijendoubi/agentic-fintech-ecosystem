//! Component liveness watchdog (spec 5.3, item 2): proves AEGIS is alive; its
//! loss trips HARD after `trip_after` of failed probes (60 s, PROPOSED).
//!
//! This is deliberately a tiny pure helper. The spec requires the watchdog to
//! run in an INDEPENDENT process (a hung Aegis cannot report itself dead), so a
//! Supervisor built on this type probes `GetKillSwitchState` and, on trip,
//! calls `TriggerKillSwitch(HARD, actor="supervisor/liveness")` and cuts the
//! broker egress. The Supervisor itself is not part of this crate (see README,
//! "Not implemented").

/// Actor id a Supervisor should use when it trips HARD on lost liveness.
pub const SUPERVISOR_ACTOR: &str = "supervisor/liveness";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LivenessWatchdog {
    last_ok_ns: i64,
    trip_after_ns: i64,
}

impl LivenessWatchdog {
    pub fn new(now_ns: i64, trip_after_ms: u64) -> LivenessWatchdog {
        let ms = i64::try_from(trip_after_ms).unwrap_or(i64::MAX / 1_000_000);
        LivenessWatchdog {
            last_ok_ns: now_ns,
            trip_after_ns: ms.saturating_mul(1_000_000),
        }
    }

    /// Record a successful probe.
    pub fn observe_ok(&mut self, now_ns: i64) {
        self.last_ok_ns = self.last_ok_ns.max(now_ns);
    }

    /// True once the probes have failed for longer than the trip window. A
    /// clock that moved backwards never un-trips it.
    pub fn should_trip(&self, now_ns: i64) -> bool {
        now_ns.saturating_sub(self.last_ok_ns) > self.trip_after_ns
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trips_only_after_the_window_and_recovers_on_ok() {
        let mut w = LivenessWatchdog::new(0, 60_000);
        assert!(!w.should_trip(60_000_000_000));
        assert!(w.should_trip(60_000_000_001));
        w.observe_ok(59_000_000_000);
        assert!(!w.should_trip(100_000_000_000));
        assert!(w.should_trip(119_000_000_001));
    }
}
