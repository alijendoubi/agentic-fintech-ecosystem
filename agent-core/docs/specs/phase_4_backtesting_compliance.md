# Phase 4 Spec — Backtesting, PTC Calibration, Audit Logger, Compliance Manifest, HITL

_Zone C (Python audit-logger, compliance-manifest; Next.js hitl-interface) + `backtesting/` (Python) | Tracks: ALI-58 (no Phase 4 spec existed before this document)_

> **Status: DRAFT specification. Nothing in this document is implemented.**
> Verified against the tree on 2026-09-19: `agent-core/backtesting/` contains only `__init__.py` and `requirements.txt`; `zone-c/audit-logger/` and `zone-c/compliance-manifest/` contain only `__init__.py` and `requirements.txt`; `zone-c/hitl-interface/` does not exist. There is no market-data dataset, no scenario database and no calibration output anywhere in the repo.
>
> Labels: **[EXISTING]** = stated in another repo document; **PROPOSED** = introduced here for the owner to decide; **TODO(owner)** = a real-world fact or decision not derivable from the repo. Regulatory statements are **requires qualified legal review**.

---

## 1. Why this phase matters

Several controls and claims elsewhere in the repo depend on numbers that do not exist yet:
- The Aegis thresholds (price collar 1.5%, daily drawdown 2.0%, order size 0.5% ADV, low-confidence omega 0.55, regime confidence) are recorded in `docs/regulatory/ptc-calibration.md` together with "results" (a 3-year P99, a 1.8% P99.9 daily loss, 500-scenario win rates) that **cannot have come from anything in this repository**. They are marked UNSUBSTANTIATED.
- The RTS 6 self-assessment, the SHARP promotion gate (Step 4) and the canary (`phase_5_canary_production.md`) all require regime-aware walk-forward analysis and Monte Carlo output.

This phase builds the tooling that **produces** those numbers from real data, with reproducibility, and the Zone C records that make decisions auditable.

## 2. Scope

| Component | Location | Status today |
|---|---|---|
| Backtest engine | `backtesting/` | empty package |
| Regime-aware walk-forward analysis (WFA) | `backtesting/` | does not exist (docs referred to `wfa.py`, which is not in the repo) |
| Monte Carlo | `backtesting/` | does not exist |
| PTC calibration procedure | `backtesting/` + `docs/regulatory/ptc-calibration.md` | figures exist without provenance |
| Audit logger + KL monitor | `zone-c/audit-logger/` | empty package |
| Compliance manifest pipeline | `zone-c/compliance-manifest/` | empty package |
| HITL interface | `zone-c/hitl-interface/` | directory does not exist (compose and CI reference it) |
| SHARP Step 4 CI hook | `.github/workflows/` (CI owner) | not present |

## 3. Data requirements

Nothing here can proceed without data; obtaining it is an owner action.

| Requirement | Detail | Status |
|---|---|---|
| Source | Docs name Polygon.io (TP-005 in the third-party register). Historical intraday bars and, if L2/quote replay is required, historical quote data | TODO(owner): confirm plan/subscription level covers the history needed and that the licence allows storing it for backtesting (register cell "Data redistribution restrictions" is unverified) |
| Depth | Existing docs cite 3-year (collar) and 5-year (drawdown, HMM) lookbacks and a 1-year HMM holdout | TODO(owner): confirm what is affordable/available; procedures below take the lookback as a parameter and report what was actually used |
| Point-in-time | Constituents, corporate actions (splits/dividends) and delistings as known at each date | Not built |
| Survivorship-bias-free universe | The EU AI Act disclosure claims a "survivorship-bias-free training universe" | Not built; that claim is marked unimplemented |
| Holdout | Last 20% of available data never seen in training or tuning [EXISTING, RTS6 template §1] | Must be enforced mechanically (dataset split manifest, held-out file not readable by training jobs) |
| Dataset manifest | Every backtest run records dataset id, SHA-256 of each input file, date range, symbol list, vendor, retrieval date | PROPOSED, required |
| Storage | Location, retention, access control | TODO(owner) |

## 4. Backtest engine requirements

### 4.1 Functional
1. **Event-driven and deterministic**: same inputs + same seed => byte-identical outputs (a run hash is recorded).
2. **Point-in-time, no look-ahead**: at time *t* the strategy sees only information stamped <= *t*. A poison test (see §10) enforces this.
3. **Transaction-cost model** integrating spread, market impact, venue fees and slippage [EXISTING, RTS6 template §1.3]. Impact functional form per `ptc-calibration.md` (square-root law) is a starting hypothesis whose coefficient must be fitted from data, not assumed.
4. **Fill simulation**: limit/market/partial fills, queue-position assumption stated explicitly, latency model (signal-to-order delay and cancel delay), rejects.
5. **Same controls as production**: the backtest must apply the Aegis pre-trade controls (`phase_3_aegis_execution.md` §4). Two independent implementations (Rust Aegis, Python engine) will drift. PROPOSED: expose the control logic as a library callable from Python, or maintain a shared golden-vector conformance suite that both implementations must pass. Choice TODO(owner).
6. **Decimal money handling** (no float for money), consistent with Phase 3 §3.
7. **Regime labels** are produced by the same HMM code as production (`zone-a/regime-detector/hmm.py`), fitted per fold on training data only (see §5).
8. Outputs: trades, equity curve, per-regime metrics, control-trigger counts, run manifest (§3), all machine-readable plus a rendered report.

### 4.2 Strategy under test (open issue)
The RTS6 template names `AFE-STRATEGY-001` "Regime-aware momentum" but **no strategy specification exists in the repo**, and the actual decision-maker is an LLM debate (Phase 2). LLM calls are non-deterministic, slow and cost money, so running the debate over years of history and 50,000 Monte Carlo paths is not feasible as stated. PROPOSED two-tier approach:
1. **Tier 1 (deterministic)**: evaluate the rubric/strategy layer, replaying recorded or cached Judge outputs where they exist, on the full history.
2. **Tier 2 (LLM-in-the-loop)**: evaluate the real debate on a bounded, budget-capped sample of historical snapshots, with cost logged per run.
TODO(owner): define what "the strategy" is for validation and where its regime applicability set `R_s` comes from (needed by the Aegis regime gate).

Validity risk to state in every report: an LLM trained on data that includes the test period can "remember" outcomes (look-ahead contamination). PROPOSED mitigation: restrict LLM-in-the-loop evaluation to periods after the model's training cutoff (TODO(owner): obtain from the vendor) and/or anonymise symbols and dates in prompts, and document residual risk.

## 5. Regime-aware walk-forward analysis

- **Folds**: rolling (or anchored) train/test windows over the history. Window lengths and step are parameters. PROPOSED starting point (to be justified in the run report, not a finding): 12-month train, 3-month test, 3-month step. TODO(owner): confirm after seeing how much data exists.
- **No leakage**: in each fold the HMM regime model is fitted on that fold's training window only; the test window's regime labels are produced by that fitted model. The engine fails the run if a label in the test window was computed with data after its timestamp.
- **Per-regime reporting** across all five states [EXISTING]: `TRENDING_BULL`, `TRENDING_BEAR`, `HIGH_VOL_CHOP`, `LOW_VOL_CHOP`, `CRISIS`. Metrics per regime: Sharpe, Sortino, Calmar, P_ruin (the columns of the SHARP CIA template), plus trade count, max drawdown, average slippage vs model, control-trigger counts.
- **Insufficient evidence is a result**: each regime needs a minimum sample (trades and calendar days; PROPOSED thresholds set in the run config and reported). A regime below the minimum is reported "insufficient evidence", never "pass". `CRISIS` will likely be rare; the report must say so instead of extrapolating.
- **Pass thresholds**: `docs/processes/sharp-promotion.md` says a change "must pass ALL regime-conditional performance thresholds", but **no such thresholds are defined anywhere in the repo**. TODO(owner): define them (they are an investment-risk decision, not something documentation can invent).
- **Overfitting control**: count every configuration tried; report a multiple-testing-adjusted statistic (e.g. a deflated-Sharpe style adjustment) alongside raw Sharpe. PROPOSED.

## 6. Monte Carlo

- 50,000 iterations [EXISTING: RTS6 template, SHARP Step 4]; P_ruin < 5% [EXISTING: RTS6 template]. **Ruin is not defined in the repo.** TODO(owner): define (e.g. drawdown from peak >= X% or NAV <= Y within horizon H) before any P_ruin is computed.
- Method: PROPOSED block bootstrap of daily (or trade-level) returns, resampled within regime, block length chosen to preserve autocorrelation, seeded, with regime transition dynamics taken from the fitted HMM. Report the estimate with a confidence interval and the sensitivity of P_ruin to block length and to a stressed cost model.
- The result feeds the RTS6 template's algorithm validation and the SHARP report; it is an estimate under stated assumptions, not a guarantee.

## 7. PTC calibration procedure (replaces the unsubstantiated figures)

Principle: every threshold in `docs/regulatory/ptc-calibration.md` is produced by a script under `backtesting/calibration/` (PROPOSED path) that consumes a versioned dataset and emits a **calibration report** containing: dataset manifest hash, date range, universe, code commit, random seed, method, the computed value **with a confidence interval**, and the value selected (with a stated safety margin and who chose it). A threshold may be listed in Aegis configuration only if a signed report for it exists. The banner on `ptc-calibration.md` is removed only when its section is regenerated from such a report.

| Control | Procedure that produces the number | Notes / known statistical issues |
|---|---|---|
| Price collar | Pull intraday bars for the universe, compute 1-minute absolute returns in regular hours, compute the P99 per symbol, then apply an explicitly stated aggregator across symbols (per-symbol collar, pooled P99, or worst-symbol). The existing doc does not say which; the choice must be recorded. Compare against a fixed floor (existing doc uses 1.0%) | Tail estimates from finite samples carry error; report CI. Any halts/auction periods handling must be stated |
| Daily max drawdown | Run the engine, build the daily P&L distribution across strategy instances and regimes, estimate a high quantile of daily loss | The existing text uses a 99.9th percentile over 5 years. Five years is roughly 1,260 trading days, so the 99.9th percentile is decided by about one observation: report a bootstrap CI and consider extreme-value methods. PROPOSED: do not present a single point estimate as "derived" |
| Order size limit | Fit the market-impact model from data (spread + participation vs realised cost); choose the participation cap where modelled impact exceeds modelled alpha for the strategy | Needs fill data. Before live trading only proxy data exists; state this. Resolve ADV window (20-day in doc vs `adv_30d` in `MarketSnapshot`) first |
| Unusual order size (P95) | Computed from AFE's own order history per symbol | **No history exists before trading.** Cold-start rule in Phase 3 spec (C19). Cannot be calibrated before the paper/canary period |
| Regime gate | Compute strategy performance conditional on regime and on HMM confidence bucket over held-out data; choose `C_MIN` and the applicability set `R_s` from the result | Resolves the contradiction between the RTS6 template (`< 0.6 OR mismatch`) and `ptc-calibration.md` (`mismatch AND > 0.70`); see Phase 3 spec §4.4. The "45% higher maximum drawdown" figure in the existing doc must be reproduced or removed |
| Low confidence (omega) | Run scenarios through the debate pipeline (or replay recorded Judge outputs), label outcomes from history, tabulate win rate and E[V] after costs by omega bucket with CIs; choose the threshold where E[V] after costs turns positive | The existing text cites 500 seed scenarios and win rates of 38/48/57/66%. **No scenario database exists.** With 500 scenarios split across four buckets, bucket samples are small: report binomial CIs. The LLM look-ahead contamination risk (§4.2) applies. Scenario provenance: TODO(owner) |

Deliverables: `calibration_report_<control>_<date>.json` and a human-readable summary committed under `docs/regulatory/calibration/` (PROPOSED path); large raw outputs stay out of git. Review cadence per `ptc-calibration.md` (at least annually [EXISTING]; the ESMA-guideline wording there requires qualified legal review).

## 8. Audit logger (`zone-c/audit-logger/`)

Requirements:
1. **Append-only and hash-chained**: each record stores `prev_hash` and `hash = SHA-256(canonical_bytes(record) || prev_hash)`. Chain over full record content, not over identifiers alone.
2. **Write-once enforced by the database**, not only by application code: the audit role has INSERT (and SELECT) but no UPDATE/DELETE/TRUNCATE; schema changes go through a separate migration role. TODO(owner): backup/WORM strategy.
3. **Ingestion**: reads Aegis's audit WAL (Phase 3 §9), kill-switch transitions, HITL actions, manifest completions, key-rotation events (runbook `key-rotation.md`).
4. **Chain verification CLI + scheduled job** that reports the first broken link; a broken chain is a Major incident (DORA doc §4) and PROPOSED to latch SOFT.
5. **KL divergence monitor**: computes KL between live and reference distributions and calls `TriggerKillSwitch`. Thresholds in compose: alert 0.1, soft switch 0.3, logic switch 0.5 [EXISTING, uncalibrated]. **The reference distribution and the quantity being monitored (omega? features? slippage?) are not defined anywhere.** TODO(owner) / to be specified and calibrated in this phase.
6. **Retention**: 7 years [EXISTING; whether that is the applicable requirement requires qualified legal review]. Compose passes `RETENTION_YEARS=7` to the manifest service only.
7. Network: compose puts `postgres-audit` on `zone-bc-audit`, a network shared with Aegis and execution-motor. Claims that Zone A/B "cannot write to Zone C" are not enforced by that topology (network-level reachability + shared credentials would suffice). PROPOSED: separate DB roles per writer and network policy so only audit-logger can reach postgres-audit. Compose owner.
8. Dependency note for the code owner: `zone-c/audit-logger/requirements.txt` lists `hashlib2`, which is not the standard-library `hashlib`; verify it is intended.

## 9. Compliance manifest (`zone-c/compliance-manifest/`)

- Assembles one `ComplianceManifest` (`shared/proto/compliance_manifest.proto`) per trade decision: snapshot, Blue/Red/Judge trace, Aegis `ControlResult`s and attestation reference, HITL record, order, post-trade metrics.
- **Chain semantics**: the proto comment says each manifest "hashes the previous manifest's ID" while the field says "SHA-256 of previous manifest". PROPOSED: hash the previous manifest's full canonical serialisation (excluding its own hash field), so content tampering breaks the chain.
- **Proto updates needed** (proto owner): money fields are `double` (`fill_price`, `arrival_price`, ...); Phase 3 §3 requires fixed-point. `PTCCheckResult` should be reconciled with Aegis `ControlResult` (Phase 3 Appendix A.1).
- **Completeness invariant**: every APPROVED order has exactly one manifest; a missing manifest after a timeout is an audit finding and PROPOSED to latch SOFT.
- The manifest is written by Zone C from data pushed by Zones A/B; contents of LLM prompts/outputs may include third-party data; retention and data-protection implications require qualified legal review.

## 10. HITL interface (`zone-c/hitl-interface/`, Next.js)

Does not exist; compose and CI reference it. Requirements:
1. Operator authentication and roles (compose passes `HITL_JWT_SECRET`; the scheme, MFA and identity source are TODO(owner)). Every action attributable to a named person.
2. Held-signal queue with countdown to `hold_expires_at_ns`; approve/reject via Aegis `ResolveHold`. Hard blocks are shown but cannot be overridden.
3. Displays an "AI-generated signal" label on every proposal [EXISTING claim in the EU AI Act disclosure; currently unimplemented].
4. Operator heartbeat control (Aegis `Heartbeat`), kill-switch trigger and multi-approver reset flow (`phase_3_aegis_execution.md` §5).
5. Reverse-guardrail distress score and cooling period appear in `HITLOverrideRecord`. No DistilBERT classifier, training data or policy exists in the repo. TODO(owner): decide whether this is in scope; if not, remove the claim from the disclosure.
6. Missing today: Dockerfile, package.json, tests. Compose publishes `3000:3000` to the host and sets `NEXT_PUBLIC_API_BASE_URL=http://localhost:4000`, an API that is not defined anywhere.

## 11. SHARP Step 4 automation

`docs/processes/sharp-promotion.md` Step 4 says backtesting is "triggered by CI/CD on proposal approval". No workflow does this. PROPOSED: a workflow triggered by an approved rubric-change proposal runs the WFA + Monte Carlo on the pinned dataset and attaches the Regime-Conditional Performance Report, with the pass thresholds defined in §5. CI owner.

## 12. Test plan

| Area | Tests |
|---|---|
| Engine | Determinism (same seed => same run hash); **no look-ahead poison test** (mutate future data; results before that time must not change); cost-model unit tests; fill-simulation edge cases (partial, rejected, halted) |
| Controls | Conformance of the backtest's controls with Aegis golden vectors |
| WFA | Fold leakage test (HMM fitted only on training window); insufficient-evidence path; per-regime metric correctness on synthetic data with known answers |
| Monte Carlo | Seeded reproducibility; known-distribution sanity checks; P_ruin convergence versus iteration count |
| Calibration | Re-running a script on the same dataset reproduces the same numbers; report schema validation; CI-width reported |
| Audit logger | Chain verification; tamper test (modify a stored record; verification pinpoints it); DB role cannot UPDATE/DELETE; KL monitor triggers on injected drift |
| Manifest | Completeness invariant; hash-chain link over full content; proto round trip |
| HITL | Playwright end-to-end: hold appears, expires, hard block not releasable, heartbeat, dual-approver reset |
| Coverage | Coverage target per repo rules (80%) measured, not asserted |

## 13. Exit criteria

- [ ] Dataset manifest exists and a holdout split is enforced.
- [ ] Engine passes determinism and no-look-ahead tests.
- [ ] WFA + Monte Carlo run end to end on real data and produce the Regime-Conditional Performance Report.
- [ ] A signed calibration report exists for every threshold configured in Aegis; `ptc-calibration.md` regenerated from them; UNSUBSTANTIATED banner removed only then.
- [ ] Ruin and pass thresholds defined by the owner (`TODO(owner)` items in §5-§6 closed).
- [ ] Audit logger verifies its chain and enforces write-once at the DB level; KL monitor's reference distribution defined and calibrated.
- [ ] Manifest completeness invariant enforced; proto updated for fixed-point money.
- [ ] HITL interface exists with the §10 features and tests.
- [ ] RTS6 self-assessment "Claims vs implementation" rows updated only where evidence exists.

## 14. Owner decisions and inputs required

1. Data source, plan level, licence, storage (§3).
2. Definition of the strategy under test and `R_s` (§4.2).
3. WFA window sizes; regime pass thresholds; ruin definition (§5-§6).
4. Aggregation rule for the collar; ADV window (§7).
5. Scenario provenance and LLM contamination handling (§4.2, §7).
6. KL monitor reference distribution and quantity (§8).
7. HITL authentication scheme; whether the distress classifier is in scope (§10).
