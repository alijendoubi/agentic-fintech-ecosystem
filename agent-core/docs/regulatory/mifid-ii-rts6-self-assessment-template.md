# MiFID II RTS 6 Annual Self-Assessment Template

**Firm:** [Firm Name]
**Assessment Period:** [YYYY-MM-DD] to [YYYY-MM-DD]
**Prepared by:** [Name, Role]
**Review Date:** [YYYY-MM-DD]
**Next Review Due:** [YYYY-MM-DD + 12 months]

---

## 1. Algorithm Inventory

| Algorithm ID | Description | Asset Class | Status | Last Validated |
|---|---|---|---|---|
| AFE-STRATEGY-001 | Regime-aware momentum (US equities) | US Equities | Active | [Date] |

### Validation Methodology
Each algorithm is validated via:
1. Regime-aware Walk-Forward Analysis (per `backtesting/wfa.py`) across all 5 HMM states
2. Monte Carlo stress testing (50,000 iterations, P_ruin < 5% threshold)
3. Full transaction cost model integration (spread + market impact + venue fees + slippage)
4. Out-of-sample holdout: last 20% of available data, never seen during training

---

## 2. Pre-Trade Controls Assessment

### 2.1 Hard Blocks

| Control | Threshold | Calibration Methodology | Data Used | Last Reviewed |
|---|---|---|---|---|
| Price collar | ±1.5% from mid-price | 99th percentile of historical intraday moves across all strategy symbols, 3-year lookback | Polygon.io daily OHLCV | [Date] |
| Daily Max Drawdown | 2.0% of starting daily NAV | Conservative; derived from 99.9% VaR at 1-day horizon over 5-year backtesting period | Internal backtesting database | [Date] |
| Order size limit | [X] shares per order | 0.5% of 20-day ADV; above this, market impact exceeds expected alpha per Square-Root Law | Polygon.io volume data | [Date] |

### 2.2 Soft Blocks

| Control | Trigger | Alert Target | Response Window | Calibration |
|---|---|---|---|---|
| Unusual order size | Top 5% of last 30-day order size distribution | HITL operator | 60 seconds | Computed weekly from order history |
| Regime mismatch | HMM confidence < 0.6 OR strategy trained regime ≠ current regime | HITL operator | Operator decision | HMM confidence threshold validated on 3-year holdout |
| Low confidence | Judge ω < 0.55 | HITL operator | Automatic abstain (no HITL required) | Threshold backtested: below 0.55 historical win rate < 50% |

---

## 3. Kill Switch Assessment

| Switch | Trigger | Tested | Last Test Date | Recovery Procedure |
|---|---|---|---|---|
| Soft Switch | Operator command or L3 Ambiguity | Yes | [Date] | Single operator authorization |
| Logic Switch | MDD breach (automated) | Yes | [Date] | Dual operator + root cause sign-off |
| Dead Man's Switch | Operator heartbeat missed > 4h | Yes | [Date] | Positive heartbeat + check-in |
| Hard Switch | Infrastructure firewall drop | Yes | [Date] | Dual operator + compliance sign-off |
| Physical Switch | BusKill USB tether | Yes | [Date] | Full incident report + DORA notification |

---

## 4. Compliance Staff Understanding

Per RTS 6, compliance personnel must have "a general understanding of how the AI impacts algorithmic decision-making."

### Training Completed

| Staff Member | Role | Training Completed | Understanding Level |
|---|---|---|---|
| [Name] | Chief Compliance Officer | [Date] | Module 1: Algorithmic trading overview; Module 2: AI-specific risks; Module 3: PTC calibration rationale |

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
| LLM inference | Anthropic via AWS Bedrock | Blue/Red/Judge/Reflector node LLMs | Yes — data processing agreement; no training on customer data |
| LLM inference | Mistral AI | Blue/Red node LLMs | Yes — commercial license; EU data residency confirmed |
| Market data | Polygon.io | Level 2 order book, trade prints | Yes — data redistribution restrictions documented |
| HSM (prod) | AWS CloudHSM | FIPS 140-2 Level 3 key custody | Yes — shared responsibility model documented |
| Co-location | [Exchange-adjacent hosting provider] | Zone B network proximity | Yes — SLA + uptime guarantees |

**Note:** The investment firm (not the software vendors) remains responsible for all MiFID II RTS 6 obligations.

---

## 6. Self-Assessment Conclusion

- [ ] All algorithms validated within the past 12 months
- [ ] All PTC thresholds calibrated with quantitative data and documented
- [ ] Kill switch procedures tested and documented
- [ ] Compliance staff training completed
- [ ] All outsourced components have compliance clauses in contracts
- [ ] DORA ICT Third-Party Register is current

**Signed:** [Name, Role] **Date:** [YYYY-MM-DD]
