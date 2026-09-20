# SHARP Rubric Promotion Gate Workflow

**SHARP:** Self-Evolving Human-Auditable Rubric Policy

> **Status: DRAFT process; not operated, no tooling exists.** Legal citations below (EU AI Act "Article 83", MiFID II RTS 6 wording) are unverified and **require qualified legal review** (see the status box in `docs/regulatory/eu-ai-act-limited-risk-disclosure.md`). Not defined anywhere in the repo, TODO(owner): the "regime-conditional performance thresholds" of Step 4 (see `docs/specs/phase_4_backtesting_compliance.md` §5), the persons filling the Compliance Officer / Legal / Risk Manager roles, and the CI job that Step 4 says triggers the backtest. The canary in Step 6 is detailed in `docs/specs/phase_5_canary_production.md`.

Every rubric change proposed by the Reflector Node must pass a mandatory human promotion gate before being deployed to the live system. This is stated to be required by (unverified, see status box):
- **EU AI Act Article 83:** Significant changes to AI system logic may require a new conformity assessment
- **MiFID II RTS 6:** All algorithm changes must be validated before activation

---

## Step-by-Step Workflow

```
Reflector Node proposes rubric change
         ↓
[Step 1] Draft Rubric Change Proposal generated (natural language)
         ↓
[Step 2] Compliance Officer review
    → Compliance Sign-off OR Rejection (with reason)
         ↓
[Step 3] Legal Assessment
    → Does this constitute a "significant change" under EU AI Act Art. 83?
    → Legal Sign-off OR Escalation to full conformity assessment
         ↓
[Step 4] Automated Backtesting (triggered by CI/CD on proposal approval)
    → Regime-aware WFA across all 5 HMM states
    → Monte Carlo (50,000 iterations)
    → Must pass ALL regime-conditional performance thresholds
    → Output: Regime-Conditional Performance Report
         ↓
[Step 5] Risk Manager Review
    → Reviews backtesting results
    → Risk Sign-off OR Rejection
         ↓
[Step 6] Canary Deploy
    → 1% of target capital, 5 trading days
    → All metrics monitored: ω distribution, venue toxicity, KL divergence, PTC trigger rate
    → Automatic promotion if canary metrics pass all thresholds
    → Manual approval required if any anomaly detected
         ↓
[Step 7] Full Promotion
    → Change deployed to 100% of capital
    → Change Impact Assessment archived to Zone C
```

---

## Change Impact Assessment Template

Every promotion generates a **Change Impact Assessment** (CIA) appended to the Zone C audit store:

```
CIA-[YYYY-MM-DD]-[RUBRIC-VERSION]

What changed:
  [Natural language description from Reflector Node]

Why:
  [Trade outcome analysis that triggered the proposal]

Regulatory Assessment:
  - EU AI Act Art. 83 significant change: [Yes/No]
  - If Yes: conformity assessment reference [CA-ID]
  - MiFID II RTS 6 impact: [Yes/No]

Backtesting Results (per regime):
  | Regime | Sharpe | Sortino | Calmar | P_ruin | Pass/Fail |
  |--------|--------|---------|--------|--------|-----------|

Canary Results:
  - Duration: [N] trading days
  - Capital: 1% of target
  - Outcome: [Pass / Anomaly detected]
  - Key metrics: [ω mean, PTC trigger rate, slippage vs. model]

Approvers:
  - Compliance Officer: [Name] [Date]
  - Legal: [Name] [Date]
  - Risk Manager: [Name] [Date]

Archived by: [System] [Timestamp]
```

---

## Promotion Rejection Handling

If any step rejects the proposal:
1. Rejection reason is logged to Zone C with timestamp and approver identity
2. Reflector Node is notified (feedback loop)
3. Rejected proposal is archived in Zone C (retained 7 years)
4. No changes are made to the live rubric

---

## Scope Clarification

**In scope for SHARP:** Changes to the active trading rubric — confidence thresholds, regime weights, position sizing rules, strategy selection logic.

**Out of scope for SHARP (require full re-deployment):**
- Changes to Aegis hard block thresholds (require PTC calibration update)
- Changes to the LangGraph graph topology (new nodes, edge rewiring)
- Changes to the HMM regime classifier (require backtesting revalidation)
