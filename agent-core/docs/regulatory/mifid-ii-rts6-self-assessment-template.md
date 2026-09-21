# MiFID II RTS 6 Annual Self-Assessment Template

> **Status: TEMPLATE / DRAFT. NOT A COMPLETED ASSESSMENT. Requires qualified legal review.**
> Nothing in this document has been assessed, tested or signed. The described controls are **target-state**: as of 2026-09-19 Aegis (the pre-trade control and kill-switch component) is a 3-line placeholder, and the backtesting engine, audit logger, compliance manifest and HITL interface do not exist. Article-level statements about what RTS 6 requires have not been verified against the regulation. Whether this template applies at all depends on whether a regulated investment firm operates the system: TODO(owner): identify the entity and its authorisation status (requires qualified legal review).

## Claims vs implementation

| Control / claim in this template | Implemented? | Tested? | Tracking |
|---|---|---|---|
| Regime-aware WFA, Monte Carlo, holdout validation (§1) | No (`backtesting/` is an empty package; `wfa.py` does not exist) | No | `docs/specs/phase_4_backtesting_compliance.md` §4-§6 |
| Price collar, drawdown, order-size hard blocks (§2.1) | No (values only as compose env; thresholds UNSUBSTANTIATED) | No | `phase_3_aegis_execution.md` C09/C15/C12; `ptc-calibration.md` banner |
| Regime mismatch, low confidence, unusual size soft blocks (§2.2) | No | No | Phase 3 C17-C19, §4.4 |
| Soft / Logic / Dead Man's / Hard / Physical kill switches (§3) | No | No | Phase 3 §5 |
| Compliance staff training (§4) | No evidence of any training or modules | n/a | TODO(owner) |
| Outsourcing contract clauses (§5) | Unverified: no contracts in repo | n/a | `third-party-ict-register.md` |
| 7-year manifest retention (via other docs) | No | No | Phase 4 §8-§9 |

**Firm:** TODO(owner): regulated entity name (none is named anywhere in the repo)
**Assessment Period:** TODO(owner): start date to end date
**Prepared by:** TODO(owner): name, role
**Review Date:** TODO(owner): date
**Next Review Due:** TODO(owner): date (the 12-month cadence itself requires qualified legal review)

---

## 1. Algorithm Inventory

| Algorithm ID | Description | Asset Class | Status | Last Validated |
|---|---|---|---|---|
| AFE-STRATEGY-001 | Regime-aware momentum (US equities) | US Equities | Not deployed (design only; no strategy specification exists in the repo: TODO(owner)) | TODO(owner): never validated |

### Validation Methodology (target-state; nothing below has been run)
Each algorithm is to be validated via:
1. Regime-aware Walk-Forward Analysis (planned in `docs/specs/phase_4_backtesting_compliance.md` §5; `backtesting/wfa.py` does not exist) across all 5 HMM states
2. Monte Carlo stress testing (50,000 iterations, P_ruin < 5% threshold)
3. Full transaction cost model integration (spread + market impact + venue fees + slippage)
4. Out-of-sample holdout: last 20% of available data, never seen during training

---

## 2. Pre-Trade Controls Assessment

### 2.1 Hard Blocks

| Control | Threshold | Calibration Methodology | Data Used | Last Reviewed |
|---|---|---|---|---|
| Price collar | ±1.5% from mid-price (UNSUBSTANTIATED, see `ptc-calibration.md`) | 99th percentile of historical intraday moves across all strategy symbols, 3-year lookback (planned; not performed) | Polygon.io daily OHLCV (none stored in repo) | TODO(owner): never reviewed |
| Daily Max Drawdown | 2.0% of starting daily NAV (UNSUBSTANTIATED) | Planned: derived from 99.9% VaR at 1-day horizon over 5-year backtesting period (no backtest exists) | Internal backtesting database (does not exist) | TODO(owner): never reviewed |
| Order size limit | Computed: shares per order = 0.5% of ADV (UNSUBSTANTIATED; ADV window conflicts, 20-day here vs `adv_30d` in code) | 0.5% of 20-day ADV; above this, market impact exceeds expected alpha per Square-Root Law (claim not evidenced) | Polygon.io volume data | TODO(owner): never reviewed |

### 2.2 Soft Blocks

| Control | Trigger | Alert Target | Response Window | Calibration |
|---|---|---|---|---|
| Unusual order size | Top 5% of last 30-day order size distribution | HITL operator | 60 seconds | Computed weekly from order history |
| Regime mismatch | HMM confidence < 0.6 OR strategy trained regime ≠ current regime **(CONTRADICTED by `ptc-calibration.md`: mismatch AND confidence > 0.70; unresolved, PROPOSED single definition in Phase 3 spec §4.4)** | HITL operator | Operator decision | Claimed: validated on 3-year holdout. **No holdout data or validation exists** |
| Low confidence | Judge ω < 0.55 | HITL operator (as stated). Note: the "Response Window" cell says automatic abstain, i.e. no HITL: internally inconsistent, TODO(owner) | Automatic abstain (no HITL required) | Claimed: backtested, below 0.55 historical win rate < 50%. **No backtest exists** (UNSUBSTANTIATED) |

---

## 3. Kill Switch Assessment

| Switch | Trigger | Tested | Last Test Date | Recovery Procedure |
|---|---|---|---|---|
| Soft Switch | Operator command or L3 Ambiguity ("L3" undefined: TODO(owner)) | Not implemented / Not tested | Never tested | Single operator authorization |
| Logic Switch | MDD breach (automated) | Not implemented / Not tested | Never tested | Dual operator + root cause sign-off |
| Dead Man's Switch | Operator heartbeat missed > 4h | Not implemented / Not tested | Never tested | Positive heartbeat + check-in |
| Hard Switch | Infrastructure firewall drop | Not implemented / Not tested | Never tested | Dual operator + compliance sign-off |
| Physical Switch | BusKill USB tether | Not implemented / Not tested | Never tested | Full incident report + DORA notification |

> **PROPOSED reconciliation note (owner to confirm; see `docs/specs/phase_3_aegis_execution.md` §5.3):** the "> 4h" Dead Man's Switch is the *operator* heartbeat. The DORA framework's "Hard Switch if unresponsive > 60s" is a separate *component liveness watchdog* on Aegis and should be listed as the Hard Switch trigger, not as a Dead Man's Switch behaviour. Drill procedure (DRAFT, unrun): `docs/runbooks/kill-switch-drill.md`.

---

## 4. Compliance Staff Understanding

Per RTS 6, compliance personnel must have "a general understanding of how the AI impacts algorithmic decision-making."

### Training Completed

| Staff Member | Role | Training Completed | Understanding Level |
|---|---|---|---|
| TODO(owner): name (ADR-001 lists the Compliance Officer as "TBD") | Chief Compliance Officer | TODO(owner): date (no training material or record exists in the repo) | TODO(owner): modules completed (the modules below are a proposed curriculum only) |

### Training Modules Available

- **Module 1:** What is algorithmic trading? How does this system trade?
- **Module 2:** AI-specific risks: regime drift, distributional shift (KL divergence), model correlated failures
- **Module 3:** PTC calibration: how are thresholds set and what quantitative data supports them?
- **Module 4:** HITL escalation: when does the system alert a human and what are the response procedures?
- **Module 5:** SHARP rubric changes: what triggers a rubric change proposal and what is the approval process?

---

## 5. Outsourcing Responsibility

| Component | Provider | Role | Compliance Clause in Contract |
|---|---|---|---|
| LLM inference | Anthropic via AWS Bedrock (route open: ADR-003 Proposed; code currently uses direct SDKs) | Judge/Compression/Reflector node LLMs (Blue/Red use Mistral per ADR-002) | UNVERIFIED (as originally written: "Yes, DPA; no training on customer data"). TODO(owner): confirm from the actual contract |
| LLM inference | Mistral AI | Blue/Red node LLMs | UNVERIFIED (as originally written: "Yes, commercial license; EU data residency confirmed"). TODO(owner): confirm |
| Market data | Polygon.io | Level 2 order book, trade prints | UNVERIFIED (as originally written: "Yes, redistribution restrictions documented"). TODO(owner): confirm |
| HSM (prod) | AWS CloudHSM (not provisioned) | FIPS 140-2 Level 3 key custody (certification claim unverified) | UNVERIFIED. No production HSM exists. TODO(owner) |
| Co-location | TODO(owner): provider, if any (none is named; nothing is deployed) | Zone B network proximity | UNVERIFIED. TODO(owner) |

**Note:** The investment firm (not the software vendors) remains responsible for all MiFID II RTS 6 obligations.

---

## 6. Self-Assessment Conclusion

- [ ] All algorithms validated within the past 12 months
- [ ] All PTC thresholds calibrated with quantitative data and documented
- [ ] Kill switch procedures tested and documented
- [ ] Compliance staff training completed
- [ ] All outsourced components have compliance clauses in contracts
- [ ] DORA ICT Third-Party Register is current

**Signed:** TODO(owner): name, role **Date:** TODO(owner): date. Do not sign until every box above is supported by evidence; as of 2026-09-19 none is.
