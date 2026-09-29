# Kill-switch drill record: 2026-09-25, DEV stack (ALI-170)

> **Environment: local dev compose stack on one Windows workstation (Docker Desktop).**
> Not the paper environment the runbook asks for: execution-motor used the in-memory mock
> broker, so there were no real paper orders to cancel. This record is evidence for the
> dev stack only. The raw output is `2026-09-25-dev-kill-switch.json` next to this file.

| Item | Value |
|---|---|
| Date / environment | 2026-09-25, dev compose stack (`docker-compose.yml` + `docker-compose.dev.yml`), mock broker |
| Code | Aegis and stack from branch `ali-20/redis-acl` (0ba87f8, stacked on PRs #12 and #13); execution-motor from PR #9 (`ali-162/motor-killswitch-cancel`, 69d02c9) |
| Harness | `agent-core/tests/live/drill_kill_switch.py` (real mTLS, real signed approvals) |
| Participants | Automated harness. Dev approvers `dev-operator-a`, `dev-operator-b`, `dev-compliance` (dev-only keys). TODO(owner): named people for a real drill |

## Observations

| Step | Observed |
|---|---|
| Baseline | NORMAL; motor not halted |
| 1 SOFT | Latched; a risk-increasing signal was REJECTED with `REASON_KILL_SWITCH_ACTIVE` only. The motor halted new submissions **16 ms** after the trigger. A single-operator reset was accepted, and the motor resumed at once |
| 2 LOGIC | The motor's cancel sweep was logged **0.14 s** after the trigger (target: 1 s). A one-operator reset was **refused**: "insufficient approvals: need 2 operator(s) and 0 compliance approver(s), distinct people". A two-operator reset was accepted |
| 6 Persistence | A SOFT latch **survived an Aegis restart** (back in 1.1 s); reset afterwards |
| 4 Freeze (liveness) | Aegis frozen with `docker pause` for 75 s. The supervisor logged 36 failed probes and 4 failed HARD deliveries while Aegis was frozen, then **latched HARD** as soon as Aegis answered again. The level after the step was HARD |

## Findings

1. **execution-motor did not notice a frozen Aegis (safety gap, PR #9).** For the whole
   75 s freeze the motor kept its last state (NORMAL) and did not halt. Its HTTP/2
   keepalive (20 s ping, 10 s timeout) did not break the idle `WatchKillSwitchState`
   stream against a paused peer. A restart (TCP reset) *was* detected immediately (step 6).
   The motor needs a detection path that does not rely on keepalive. Tracked in ALI-162.
2. **A frozen Aegis is only latched HARD after it resumes.** The supervisor cannot deliver
   HARD to a frozen process. It retries, and exits non-zero if delivery stays impossible
   for a second full window. While frozen, Aegis signs nothing, so no new order can be
   authorised. Orders already at the broker are protected only by the motor, which is
   finding 1.
3. **Dev resets were impossible before this change.** The dev identities had no
   `kill-reset` peer and no approvers, so any dev trip could only be cleared by wiping
   Aegis state. `dev-tls/generate-dev-certs.sh` now issues an `operator` identity and
   three dev approver keys.

## Rerun after the fix (same day)

Evidence: `2026-09-25-dev-kill-switch-rerun.json`. execution-motor rebuilt with the unary
liveness probe (PR #9, 786f3c6).

| Check | Observed |
|---|---|
| HARD reset (the supervisor's latch from the first run) | Accepted with **two operators plus compliance**, the authority the spec requires for HARD |
| Steps 1, 2, 6 | Same results as the first run |
| Freeze, 30 s | The motor logged the state stream down and **halted after 5.3 s**, then resumed as soon as Aegis answered. The supervisor did not latch HARD, as expected: 30 s is shorter than its 60 s window |

Finding 1 is resolved for the dev stack.

## Not covered (needs the paper environment or owner input)

- Real open paper orders cancelled at LOGIC (the mock broker has none).
- DEAD MAN'S (step 3), PHYSICAL (step 5), HARD reset with compliance sign-off, broker
  egress cut (not implemented), audit-chain verification over the drill period.
- Named participants and sign-off (TODO(owner)).
