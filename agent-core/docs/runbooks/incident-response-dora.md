# Runbook: ICT Incident Response (DORA-oriented)

> **DRAFT: not validated in any drill. UNTESTED.**
> **Requires qualified legal review before use.** Reporting obligations depend on whether the operating entity is an in-scope financial entity, on its regulator, and on the applicable delegated regulations and thresholds. The timelines and classification below are copied from `docs/regulatory/dora-ict-risk-management-framework.md` §4 and have **not** been verified against the regulation (in particular, the event that starts each clock and the classification criteria). The Zone C "auto-generated initial notification template" mentioned in that document does not exist. Evidence tables are intentionally blank.

## Purpose

A step-by-step process to detect, contain, classify, escalate and learn from ICT incidents affecting the trading system, with a place to record what actually happened.

## Roles (TODO(owner): assign real people; the docs assume roles that may not exist)

| Role | Person |
|---|---|
| Incident commander | TODO(owner) |
| Operator(s) with kill-switch authority | TODO(owner) |
| Compliance officer (ADR-001 lists "TBD") | TODO(owner) |
| Legal contact | TODO(owner) |
| Regulator contact / competent authority, and notification channel | TODO(owner): unknown which authority applies |

## 1. Detect and triage (first minutes)

1. Record detection time (`T_detect`), source (alert, operator, audit chain check, broker, vendor) and reporter.
2. Open an incident record (fields in the evidence table).
3. **Contain first**: if orders may be wrong, unattested or unreconciled, latch the appropriate kill switch (`docs/specs/phase_3_aegis_execution.md` §5; drill in `kill-switch-drill.md`) and cancel open orders. Remember the HARD-switch caveat: open broker orders need manual cancellation.
4. Preserve evidence: do not restart/redeploy before exporting logs, audit chain range, manifests and broker statements.

## 2. Classify (draft criteria from the DORA doc §4, unverified)

| Category | Draft definition | Examples |
|---|---|---|
| Major | Significant disruption to trading operations, regulatory breach, or data-integrity compromise | Aegis crash during market hours; audit chain broken; unattested order reaching the broker; credential compromise |
| Minor | Short disruption (< 15 min), no regulatory impact, automatic recovery | Redis restart; QuestDB reconnect |

Classification decision, who made it and when: record it. When unsure, treat as Major until legal/compliance review says otherwise (PROPOSED).

## 3. Notify (draft timelines from DORA doc §4 for Major incidents; requires qualified legal review)

| Report | Deadline stated in the doc | Recipient | Content |
|---|---|---|---|
| Initial notification | 4 hours from detection | Competent authority | Incident id, classification, initial impact |
| Intermediate report | 72 hours from detection | Competent authority | Root cause (if known), containment, updated impact |
| Final report | 1 month from containment | Competent authority | Root cause analysis, remediation, lessons learned |

Open questions for counsel: which clock start event applies (detection vs classification as Major); whether these are the correct intervals; the applicable authority, template and channel; whether client/counterparty notification is needed; whether any US-side notifications (broker, regulator) apply; data-protection breach duties if personal data is involved.

Also notify: the broker (if orders/credentials are involved) and affected ICT third parties per their contracts (TODO(owner): contact details from the register once verified).

## 4. Eradicate and recover

Use `dr-failover.md` (recover into a halted state; release only by the checklist) and `key-rotation.md` (emergency rotation if credentials or keys may be exposed).

## 5. Post-incident review

Within a period the owner sets (PROPOSED: 5 working days): root cause, contributing factors, control gaps, corrective actions with owners and dates, updates to specs/runbooks/regulatory documents, and any changes required to the third-party register. Feed lessons into drills. If the incident was caused by a rubric change, record it in the SHARP process (`docs/processes/sharp-promotion.md`).

## 6. Tabletop exercise (to validate this draft)

Suggested scenarios: (a) audit chain verification reports a broken link; (b) broker key found in a log; (c) Aegis approves an order that violates a limit; (d) LLM provider outage during market hours. Walk through §1-§5 with the named roles, timing each step, and record deviations.

## Evidence to record per incident or exercise (fill only from observation)

| Item | Value |
|---|---|
| Incident id, exercise or real | |
| `T_detect`, detection source | |
| Containment actions and times | |
| Classification (Major/Minor), decided by, time | |
| Notifications sent (recipient, time, reference); or "none required" with the advice relied on | |
| Evidence preserved (location, hashes) | |
| Recovery completed at; release checklist reference | |
| Root cause | |
| Corrective actions (owner, due date) | |
| Runbook corrections made | |
| Sign-off (name, date) | |
