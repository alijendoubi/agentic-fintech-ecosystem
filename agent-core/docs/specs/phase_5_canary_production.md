# Phase 5 Spec — Canary and Production Readiness

_Ops, infrastructure and governance | Tracks: ALI-58_

> **Status: DRAFT specification. Nothing in this document is implemented, and nothing in it is a recommendation to trade real money.**
>
> **Whether to go live at all is an owner decision.** A "no-go", or an indefinite stay in paper trading, is a legitimate outcome. Completing every checklist item below establishes only that the listed prerequisites were met; it does not establish that the system is profitable, safe or lawful to operate. Items about authorisation, registration, licensing, market-access obligations and testing obligations are **requires qualified legal/regulatory advice** and are listed as questions, not answers.
>
> Labels: **[EXISTING]** = stated in another repo document; **PROPOSED** = introduced here for the owner to decide; **TODO(owner)** = a fact or decision not derivable from the repo.

---

## 1. Preconditions (nothing here starts until all are true)

1. Phase 3 exit criteria met (`phase_3_aegis_execution.md` §12), including a recorded kill-switch drill in the paper environment.
2. Phase 4 exit criteria met (`phase_4_backtesting_compliance.md` §13): calibrated, signed PTC thresholds; audit logger and manifest working; HITL interface working.
3. A paper-trading soak completed (Stage 0 below).
4. ADR-003 (LLM access) and ADR-004 (HSM signing vs broker authentication) decided by the owner, not left in "Proposed".
5. Broker decision made (below).

Current reality (2026-09-19): none of 1-5 is true. Aegis is a placeholder; there is no backtest engine, audit logger or HITL UI.

## 2. Staged rollout

| Stage | Description | Capital at risk | Duration | Exit |
|---|---|---|---|---|
| 0. Paper soak | Full pipeline against the broker's paper environment | none | PROPOSED: at least 20 trading days. Rationale: enough sessions to see several regimes of intraday behaviour and to exercise reconciliation, kill switches and operations; the number itself is a judgement call for the owner | Stage 0 metrics within bands (§4), zero abort conditions, drills done |
| 1. Canary | 1% of target capital for 5 trading days [EXISTING: `docs/processes/sharp-promotion.md` Step 6] | 1% of target capital | 5 trading days [EXISTING] | Promotion or abort (§4-§5) |
| 2. Promotion | Increase to target capital | per owner | n/a | Owner decision + sign-offs (§7) |

Facts the owner must supply: **target capital** (TODO(owner): not stated anywhere, so "1%" has no absolute value yet) and whether the canary trades real money at a live broker account (the SHARP text implies yes).

What a 5-day canary can and cannot show: with few trades per day, five sessions cannot establish profitability or statistically validate alpha. It can validate plumbing and controls: order flow, attestation checks, reconciliation, audit completeness, slippage versus the cost model, kill-switch behaviour, operator procedures. Success criteria below are therefore operational, not performance targets.

Note on scope: SHARP Step 6 defines the canary for *rubric changes* promoted to a running system. The first-ever live canary is a broader event (new infrastructure, new keys, new broker account). This spec applies the same canary shape to it and additionally requires §6-§8.

## 3. Metrics to monitor (all logged to Zone C)

Names follow SHARP Step 6 [EXISTING]: omega distribution, venue toxicity, KL divergence, PTC trigger rate. Operational metrics added here are PROPOSED.

| Metric | Definition | Baseline source |
|---|---|---|
| omega distribution | Distribution (mean, quantiles) of Judge omega on submitted signals | Phase 4 backtest + Stage 0 |
| PTC trigger rate | Rejections/holds per control (C01-C19) per 100 signals | Phase 4 backtest + Stage 0 |
| KL divergence | Live vs reference distribution per the audit-logger monitor | Reference to be defined (Phase 4 §8 item 5) |
| Venue toxicity | Per SHARP; scoring method not specified anywhere yet (TODO(owner)); may be moot with a single broker | n/a |
| Slippage vs model | Realised slippage minus modelled cost, in bps, per order | Phase 4 cost model |
| Order-to-fill and decision latency | p50/p99 of Aegis decision time (< 50 ms target, Phase 3 §8), signal-to-order time | Phase 3 measurements |
| Reconciliation | Count/size of differences between Aegis position ledger and broker positions | must be zero |
| Audit completeness | Orders without a manifest; audit chain verification result | must be zero / valid |
| Broker rejects/errors | Rate and reasons | Stage 0 |
| Kill-switch events | Any latch, cause, time-to-effect | must be explained |
| Drawdown | Intraday and cumulative versus the drawdown control | Phase 4 calibration |
| Operator load | Held signals per day, mean time to human decision | Stage 0 |

## 4. Promotion and abort thresholds (all PROPOSED)

The absolute numbers depend on Phase 4 outputs that do not exist. Thresholds are expressed relative to the Stage 0/backtest baseline so no figure is invented here; the owner sets the tolerance.

**Automatic abort (latch LOGIC or higher, stop trading, cancel open orders, incident process):**
- Daily drawdown control reached (existing control).
- Any Aegis-versus-broker reconciliation difference not explained within the reconciliation interval.
- Any order at the broker without a valid Aegis attestation (must be impossible; treat as a security incident).
- Audit chain verification failure or any missing manifest.
- A kill-switch drill or real event whose time-to-effect exceeds the Phase 3 target.
- Broker credential/HSM failure that forces HARD.

**Hold and manual review (no promotion until a human signs off, as in SHARP Step 6 "manual approval required if any anomaly detected"):**
- KL divergence at or above the soft threshold (0.3 [EXISTING, uncalibrated]).
- PTC trigger rate outside the baseline band. Band = baseline +/- k standard deviations, k TODO(owner) (PROPOSED starting value: 2).
- omega mean or quantiles outside the baseline band (same rule).
- Slippage versus model outside tolerance (tolerance TODO(owner)).
- Any unexplained HITL escalation pattern.

**Promotion (all required):** five trading days completed; no abort condition; no unresolved hold; all §3 metrics inside their bands; drills current (§8); sign-offs per §7.

## 5. Abort and rollback procedure

1. Latch the appropriate kill switch (`phase_3_aegis_execution.md` §5); cancel open orders (note the HARD-switch caveat: open orders cannot be cancelled through the system once egress is cut, so the manual broker procedure must exist and be rehearsed: TODO(owner)).
2. Decide on position handling (flatten vs hold) explicitly; there is no automatic policy (Phase 3 §5.2, Dead Man's row).
3. Preserve evidence: audit chain export, manifests, broker statements.
4. Classify the event under the DORA doc §4 criteria and, if Major, start the reporting clock per `docs/runbooks/incident-response-dora.md` (timelines there require qualified legal review).
5. Post-incident review; record outcome in the SHARP Change Impact Assessment or an incident record.

## 6. Production infrastructure prerequisites

None of the following exists in the repo (there is no IaC, no CloudHSM configuration, no monitoring stack).

| Area | Prerequisite | Status / open question |
|---|---|---|
| Runtime platform | Today: local Docker Compose. Production target not decided | TODO(owner): AWS is assumed by ADR-001/002 but no account, region or architecture is defined |
| Infrastructure as code | All production infrastructure reproducible from code, reviewed, versioned | Not started; tool TODO(owner) |
| HSM | Production HSM per ADR-001 (AWS CloudHSM), subject to ADR-004. HA topology, backup/restore, user/role model, PIN handling | Not started. Cost and cluster requirements: TODO(owner) verify. ADR-001's 2-of-3 MPC threshold design is unverified |
| Key custody and rotation | Rotation every 90 days [EXISTING, ADR-001], emergency rotation; procedure in `docs/runbooks/key-rotation.md` (DRAFT). What is rotated depends on ADR-004 (broker key is issued by the broker, not by the HSM) | Untested |
| Secrets | No plaintext secrets in env files in production; secret manager; least privilege | Compose `.env` model is dev-only |
| LLM access | Per ADR-003 outcome; DPAs and data-residency confirmed | Open |
| Network | Zone segmentation enforced by real network policy (VPC/security groups), egress allow-lists, mTLS between zones, no host-published internal ports | Compose topology has known gaps (Phase 3 §14) |
| Observability | Metrics, logs, traces, alerting, dashboards for §3; alert routing to a human | Not started |
| Backups and DR | Backups of QuestDB, audit DB (point-in-time recovery), HSM; RTO/RPO per DORA doc §5 [EXISTING, unvalidated] | Untested |
| Supply chain | Pinned images (compose uses `chromadb/chroma:latest`), dependency scanning, SBOM | Not started |
| Time | Trusted time source; skew monitoring (Phase 3 §9) | Not started |
| On-call and staffing | Named people who hold the operator heartbeat role, can reset kill switches and respond to incidents, including cover for absences | TODO(owner): the docs assume several roles (Compliance Officer, Legal, Risk Manager, dual operators). Who fills them is not stated |

## 7. Governance and sign-offs

SHARP names Compliance Officer, Legal and Risk Manager as approvers [EXISTING]. ADR-001 lists the Compliance Officer as "TBD". The persons (or the fact that some roles may not exist) are TODO(owner). A go decision on production capital requires, at minimum, a recorded, dated decision by the owner with the evidence listed in §9 attached. Do not backfill approvals.

## 8. Disaster-recovery and operational drills

Schedule stated in the DORA doc §5 [EXISTING; not validated by any drill]: monthly Soft + Logic switch drill; quarterly full-system restart with RTO measurement; annual TLPT (applicability and frequency: requires qualified legal review).

All procedures are in `docs/runbooks/`, each labelled DRAFT and untested:
- `kill-switch-drill.md`
- `key-rotation.md`
- `dr-failover.md`
- `incident-response-dora.md`

Before any real-money stage: at least one execution of each runbook in the paper environment, evidence recorded exactly as specified in each runbook, and the runbook corrected where reality differed. No result may be recorded that was not observed.

## 9. Go / no-go checklist

Every row needs evidence attached (link to file, log, report, or dated record). Status today is "not started" for every row unless stated.

| # | Item | Evidence required | Status |
|---|---|---|---|
| 1 | Owner has decided to proceed toward live trading | Dated owner decision record | Not decided |
| 2 | Broker decision | Chosen production broker; account type; agreement reviewed; API authentication model documented (ADR-004); paper vs live endpoint controls. Alpaca is the working choice; the DORA doc says production broker "TBD" | Open: TODO(owner) |
| 3 | Phase 3 exit criteria | Test results, latency measurements | Not met |
| 4 | Phase 4 exit criteria | Signed calibration reports; WFA/MC report | Not met |
| 5 | ADR-003 and ADR-004 decided | Status changed from Proposed by the owner | Open |
| 6 | Kill-switch drill (paper) | Completed runbook evidence | Not run |
| 7 | Key rotation drill (paper/dev HSM) | Completed runbook evidence | Not run |
| 8 | DR failover drill | Completed runbook evidence incl. measured RTO/RPO | Not run |
| 9 | Incident-response tabletop | Completed runbook evidence | Not run |
| 10 | Production HSM/IaC/monitoring in place | IaC state, HSM config, alert test | Not started |
| 11 | Third-party register verified | Each row's contract facts confirmed from actual contracts; every `Last Reviewed` filled | Unverified: see register status box |
| 12 | Regulatory hygiene | Regulatory docs updated, "Claims vs implementation" tables reflect reality | Partially done in this docs pass (tables added; controls unimplemented) |
| 13 | **Legal/regulatory advice obtained** on: (a) which regulator(s) and regimes apply to the operating entity and its trading (e.g. MiFID II RTS 6 algorithmic-trading obligations, DORA, US rules for market access and short selling); (b) whether any authorisation, registration, notification or membership is required to trade in this manner; (c) whether TLPT applies; (d) EU AI Act status and dates; (e) data-protection position for LLM prompts | Written advice from qualified counsel | Not obtained. **requires qualified legal review**; this repo makes no claims on these points |
| 14 | Trading capital and loss limits | Owner-set target capital, maximum acceptable loss, and the canary size in absolute terms | TODO(owner) |
| 15 | People | Named operators, dual-approval pairs, on-call rota, cover | TODO(owner) |
| 16 | Canary stage results | Completed §3 metrics report, promotion/abort record | Not run |

## 10. Post-canary

- Produce a Change Impact Assessment per `docs/processes/sharp-promotion.md` with real numbers only.
- Update the RTS6 self-assessment, DORA framework and third-party register with dated, evidence-backed entries (no placeholders left silently).
- Reconvene the go/no-go review before increasing capital beyond the canary.
