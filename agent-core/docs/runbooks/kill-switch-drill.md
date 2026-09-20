# Runbook: Kill-Switch Drill

> **DRAFT: not validated in any drill. UNTESTED.**
> The kill switches described here are defined in `docs/specs/phase_3_aegis_execution.md` §5 and **do not exist yet** (Aegis is a placeholder). This runbook is a draft procedure to be executed and corrected once they are implemented. Every timing below is PROPOSED. **No drill has ever been run**; the evidence tables at the end are intentionally blank. Never fill them with anything that was not observed.

## Purpose

Prove, in the **paper** environment, that each kill-switch level takes effect within its target time, blocks what it should, persists across restart, and can be reset only by the required authority.

## Frequency

DORA doc §5 states a monthly Soft + Logic drill [EXISTING, unvalidated]. Hard/Physical and restart persistence are PROPOSED quarterly or after any Aegis change.

## Preconditions

- [ ] Paper broker endpoint only (`ALPACA_BASE_URL` = paper). Confirm before starting; abort if a live endpoint is configured.
- [ ] Named participants: operator A, operator B (for dual-control resets), compliance signer (for Hard reset), observer recording evidence. TODO(owner): names.
- [ ] Baseline: `GetKillSwitchState` = NORMAL; `GetAegisState` healthy (HSM ok, audit sink ok).
- [ ] Some open paper orders and at least one paper position exist to make cancel-all observable.
- [ ] Audit logger running and chain verification passing before the drill.
- [ ] Time source synchronised on all hosts; clock offset noted.

## Procedure

For each step record `T0` (action time), `T1` (observed effect time) and the delta; capture logs/screenshots/audit record ids.

### Step 1: SOFT
1. Operator A issues `TriggerKillSwitch(SOFT)`.
2. Submit a risk-increasing test signal: expect `REJECTED` with `REASON_KILL_SWITCH_ACTIVE`. Submit a risk-reducing signal: expect normal evaluation.
3. Reset with a single operator authorisation. Expect NORMAL and an audit record.

### Step 2: LOGIC
1. Trigger `LOGIC` manually (and, separately, by injecting a simulated drawdown breach in the paper account/test harness).
2. Expect: new signals rejected; **all open paper orders cancelled** within the target (PROPOSED <= 1 s); state stream delivered to execution-motor.
3. Try to reset with one operator only: expect refusal, audited. Reset with two distinct operators plus root-cause reference: expect success.

### Step 3: DEAD MAN'S
1. Configure a short heartbeat interval in the test environment only (a shortened value is a test parameter, not the production value).
2. Stop sending heartbeats. Expect warning, then trip at the deadline; behaviour as LOGIC with no human-released orders.
3. Send a positive heartbeat plus check-in: expect reset per the table.

### Step 4: HARD (liveness watchdog)
1. Suspend or block the Aegis process (do not kill it cleanly).
2. Expect Supervisor to trip HARD after the watchdog window (target 60 s) and cut broker egress within its target.
3. Confirm the execution path cannot reach the broker. **Record how the open broker orders were handled**; verify the manual broker-side cancel procedure works (TODO(owner): write the actual procedure for the paper account first).
4. Reset with dual operator + compliance sign-off; restore egress; verify state.

### Step 5: PHYSICAL
1. Disconnect the BusKill tether on the operator workstation used for the drill (or simulate the trigger script if the hardware is not present; label the evidence "simulated").
2. Record exactly what the configured action did (this behaviour is undefined in existing docs: TODO(owner)).
3. Reset requires a full incident report and DORA notification per the RTS6 template §3 [EXISTING]; for a drill, record a drill incident report and state that no real notification was made.

### Step 6: Persistence and fail-closed
1. With a latch active, restart Aegis: expect the level unchanged.
2. Make the state store unreadable in the test environment: expect Aegis to start at HARD.
3. Remove the HSM token: expect no attestation, `REASON_HSM_UNAVAILABLE`.

### Step 7: Close out
- [ ] All latches cleared by authorised resets; state NORMAL.
- [ ] Audit chain verification passes over the drill period.
- [ ] Deviations from this runbook written up and the runbook corrected in a follow-up commit.

## Pass criteria (PROPOSED)

Each level blocked what the spec says within its target time; resets required exactly the stated authority; state persisted across restart; no order reached the (paper) broker without an attestation; audit records complete. Any failure is a finding, not a pass.

## Evidence to record (fill only from observation)

| Item | Value |
|---|---|
| Drill date / environment / commit hash of Aegis and execution-motor | |
| Participants (names, roles) | |
| Step 1 SOFT: T0, T1, delta, audit record ids | |
| Step 2 LOGIC: T0, T1 (state), T1 (orders cancelled), reset approvers | |
| Step 3 DEAD MAN'S: configured interval, trip time, reset | |
| Step 4 HARD: probe window, trip time, egress cut time, broker order handling | |
| Step 5 PHYSICAL: real or simulated, behaviour observed | |
| Step 6: level after restart; start-up level with unreadable state | |
| Audit chain verification output (file/hash) | |
| Deviations and runbook corrections | |
| Sign-off (name, date) | |
