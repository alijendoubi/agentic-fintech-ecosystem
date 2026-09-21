# Runbook: Disaster Recovery and Failover

> **DRAFT: not validated in any drill. UNTESTED.**
> The RTO/RPO figures come from `docs/regulatory/dora-ict-risk-management-framework.md` §5 and are **targets that have never been measured**. Several components named here do not exist yet (Aegis, execution-motor, audit-logger, compliance-manifest, hitl-interface). No production environment exists. Evidence tables are intentionally blank.

## Purpose

Restore trading capability safely after component or site failure, and measure actual RTO/RPO against the targets.

## Targets under test [EXISTING, unvalidated]

| Component | RTO | RPO | Stated procedure (DORA doc §5) |
|---|---|---|---|
| Aegis + HSM | 15 min | 0 | Restart container; HSM tokens volume-mounted |
| Cognitive Core | 30 min | Last checkpoint | Restart LangGraph with checkpoint restore (no checkpointing exists in code: TODO(owner)) |
| QuestDB | 1 h | Last WAL segment | Volume restore from snapshot |
| Audit Logger / audit DB | 4 h | Last committed row | PostgreSQL point-in-time recovery (compose runs `wal_level=logical`; no archiving/PITR is configured: TODO(owner)) |
| Full system | 2 h | n/a | Docker Compose up; health checks |

Note: DORA doc §3.2 mentions a "multi-region failover plan" for AWS. No such plan exists in the repo. TODO(owner): decide whether a second region/site is in scope.

## Governing rule

**Recover into a halted state, then release deliberately.** After any failure, the system comes back at a kill-switch level of at least SOFT (PROPOSED; Aegis must in any case never restart below its persisted level, Phase 3 §5.1) and trading resumes only after the checklist below is complete and an operator confirms.

## Scenarios

### S1: Aegis crash or HSM unavailable
1. Confirm state: no attestations issued; Supervisor status (target: HARD after 60 s of unresponsiveness).
2. Restart Aegis; verify persisted kill level; verify HSM session/token (`GetAegisState.hsm_ok`).
3. Reconcile: compare Aegis position ledger with broker positions and open orders; resolve differences manually before release.
4. Reset latches per `kill-switch-drill.md` authorities; resume.

### S2: QuestDB loss or corruption
1. Halt new signals (SOFT). Regime detector and sensory-array lose their store; Aegis fails closed on stale reference data.
2. Restore volume from the latest snapshot (snapshot mechanism and cadence: TODO(owner)); record snapshot time (= RPO achieved).
3. Restart sensory-array; verify fresh snapshots and TTL flags; regime-detector may need bootstrap (Phase 1 spec: 30 minutes of streaming data unless a model file exists).
4. Release when reference data is fresh.

### S3: Audit database loss
1. A missing audit store means decisions cannot be recorded: Aegis fails closed (`REASON_AUDIT_UNAVAILABLE`). Latch SOFT.
2. Restore from backup/PITR; run **audit chain verification** across the restored range; any gap is a Major incident (DORA doc §4).
3. Re-ingest Aegis WAL for the gap; verify completeness invariant (one manifest per approved order).

### S4: Cognitive Core / LLM outage
1. Expected behaviour: no signals, hence no trades; system abstains. No Aegis action needed.
2. Restart; check the LLM route per ADR-003's chosen option; resume when the debate graph completes end to end on a test snapshot.

### S5: Host or site loss (full system)
1. Bring up infrastructure from code (IaC does not exist yet), restore volumes (QuestDB, audit DB, HSM tokens or HSM cluster access), restore secrets from the secret store.
2. Start Zone C first (audit), then Zone B (Aegis, sensory-array, execution-motor/gateway), then Zone A.
3. Reconcile with the broker before any release.

### S6: Broker connectivity loss or broker outage
1. Execution-motor detects ACK timeouts; latch SOFT; do not "route around" with a second broker unless one has been contracted and authorised (none is; TODO(owner)).
2. On recovery reconcile fills, orders and positions before release.

### S7: Operator unavailable
Dead Man's Switch trips (LOGIC-equivalent). Recovery requires a positive heartbeat + check-in.

## Release checklist (all required)

- [ ] Kill level state known, latches understood; resets performed by the required authority.
- [ ] Reference data fresh; regime labels flowing.
- [ ] Aegis `GetAegisState`: HSM ok, audit sink ok, limits config hash matches the approved config.
- [ ] Audit chain verification passes; no manifest gaps.
- [ ] Broker reconciliation: positions and open orders match.
- [ ] Incident record opened if the event met DORA doc §4 "Major" criteria (see `incident-response-dora.md`).
- [ ] Operator explicitly confirms resumption; recorded.

## Drill schedule (from DORA doc §5, unvalidated)

Quarterly full-system restart with RTO measurement. PROPOSED: drill S1, S2 and S3 individually first, in the paper environment, then S5.

## Evidence to record when a drill is executed (fill only from observation)

| Item | Value |
|---|---|
| Date, environment, commit hashes, participants | |
| Scenario | |
| Failure injected at (time) | |
| Detection time and how detected | |
| Service restored at (time) | |
| Measured RTO per component vs target | |
| Data loss observed (RPO) and how measured | |
| Reconciliation result (differences found, resolution) | |
| Audit chain verification output | |
| Steps that did not work as written; runbook corrections | |
| Sign-off (name, date) | |
