# DORA ICT Risk Management Framework
## Digital Operational Resilience Act (Regulation 2022/2554)
**Effective since: January 17, 2025**

> **Status: DRAFT framework. Describes target-state controls, not implemented ones. Requires qualified legal review.**
> As of 2026-09-19 Aegis, execution-motor, audit-logger, compliance-manifest and hitl-interface are empty or missing; no DR/restore, incident-reporting or key-rotation procedure has ever been exercised (DRAFT runbooks: `docs/runbooks/`). Whether DORA applies to the operating entity, which articles apply, incident classification criteria, reporting intervals and TLPT applicability/frequency all **require qualified legal review** and have not been verified here. TODO(owner): identify the operating entity and its regulatory status.

## Claims vs implementation

| Control claimed | Implemented? | Tracking |
|---|---|---|
| Aegis crash: Dead Man's Switch / Hard Switch after 60 s (§3.1) | No. Also conflates two mechanisms (see note in §3.1) | `docs/specs/phase_3_aegis_execution.md` §5.3 |
| HSM unavailable: trading halted; fallback to SoftHSM2 (§3.1) | No. (A software HSM fallback for production is itself a security question: TODO(owner)) | Phase 3 §9; ADR-004 |
| KL divergence Soft -> Logic cascade (§3.1) | No; reference distribution undefined | `phase_4_backtesting_compliance.md` §8 |
| Zone C append-only + hash chaining; Zone A/B cannot write to Zone C (§3.1) | No. Compose puts Aegis/execution-motor on the same network as the audit database | Phase 4 §8 |
| Exchange connectivity loss: cancel + re-route via SOR (§3.1) | No SOR exists; no second venue/broker contracted | Phase 3 §10; `dr-failover.md` S6 |
| Multi-region failover; Alpha Vantage fallback (§3.2) | No plan or implementation | TODO(owner); Phase 5 §6 |
| Zone C auto-generates the initial incident notification (§4) | No | `incident-response-dora.md` |
| RTO/RPO table and DR test schedule (§5) | Unmeasured, no drill run | `dr-failover.md` (DRAFT) |
| Third-party register with contract clauses (§6) | Unverified: no contracts in repo | `third-party-ict-register.md` |
| 90-day key rotation, 4 h emergency rotation (§7) | No keys provisioned; procedure untested | `key-rotation.md` (DRAFT); ADR-004 |

---

## 1. Scope

This framework applies to the Agentic Fintech Ecosystem (AFE) autonomous trading system. It documents the ICT risk management controls required under DORA Article 5–16 for any EU financial entity deploying or using this system.

---

## 2. ICT Asset Inventory

| Asset | Zone | Classification | Owner | Criticality |
|---|---|---|---|---|
| LangGraph cognitive-core | Zone A | ICT Service | Ali Jendoubi | High |
| HMM Regime Detector | Zone A | ICT Service | Ali Jendoubi | High |
| ChromaDB Vector DB | Zone A | ICT Asset | Ali Jendoubi | High |
| Rust Sensory Array | Zone B | ICT Service | Ali Jendoubi | Critical |
| Aegis PTC Engine | Zone B | ICT Service | Ali Jendoubi | Critical |
| HSM (SoftHSM2/CloudHSM) | Zone B | ICT Asset | Ali Jendoubi | Critical |
| FIX/Alpaca Execution Motor | Zone B | ICT Service | Ali Jendoubi | Critical |
| QuestDB | Zone A/B | ICT Asset | Ali Jendoubi | High |
| Redis | Zone A/B | ICT Asset | Ali Jendoubi | High |
| Audit Logger | Zone C | ICT Service | Compliance Officer | Critical |
| Compliance Manifest | Zone C | ICT Service | Compliance Officer | Critical |
| HITL Interface | Zone C | ICT Asset | Compliance Officer | High |
| PostgreSQL (audit) | Zone C | ICT Asset | Compliance Officer | Critical |

---

## 3. ICT Risk Assessment

### 3.1 Key Risk Scenarios

| Risk | Probability | Impact | Control |
|---|---|---|---|
| Zone A LLM API outage | Medium | High | Fallback to abstain (no trade); auto-restart via Docker |
| Zone B Aegis crash | Low | Critical | Dead Man's Switch activates; Hard Switch if unresponsive > 60s **[PROPOSED reconciliation, owner to confirm: the 60 s figure belongs to an Aegis *liveness watchdog* that trips the Hard Switch; the Dead Man's Switch is the 4 h *operator* heartbeat of the RTS6 template. See `docs/specs/phase_3_aegis_execution.md` §5.3. Wording above is left as originally written until the owner decides.]** |
| HSM token unavailable | Low | Critical | Trading halted immediately; fallback to SoftHSM2 (dev) |
| KL divergence breach | Medium | High | Automated Soft Switch → Logic Switch cascade per thresholds |
| Zone C audit log compromise | Very Low | Critical | Append-only + hash chaining; Zone A/B cannot write to Zone C |
| Exchange connectivity loss | Medium | High | Execution Motor detects ACK timeout; cancel + re-route via SOR |

### 3.2 Concentration Risk

Third-party concentration risks:
- AWS (Bedrock + CloudHSM + VPC): mitigated by multi-region failover plan
- Polygon.io (primary market data): mitigated by Alpha Vantage fallback for cold path
- Alpaca (execution broker): the working choice for paper trading; TODO(owner): production broker decision (Phase 5 go/no-go item 2). Paper trading is not a concentration mitigation

---

## 4. ICT Incident Classification & Reporting

### Classification Criteria

| Category | Definition | Example |
|---|---|---|
| **Major** | Significant disruption to trading operations, regulatory breach, or data integrity compromise | Aegis crash during market hours, audit log chain broken |
| **Minor** | Short disruption (<15min) with no regulatory impact and automatic recovery | Redis restart, QuestDB reconnection |

### Reporting Timeline (Major Incidents)

> Requires qualified legal review: the intervals below, the event that starts each clock (detection vs. classification as major), the classification criteria above, and the competent authority/channel have not been verified against the regulation and its delegated acts. Do not rely on them. TODO(owner): obtain counsel's confirmation.

| Report | Deadline | Recipient | Content |
|---|---|---|---|
| Initial Notification | 4 hours from detection | Competent authority (NCA) | Incident ID, classification, initial impact assessment |
| Intermediate Report | 72 hours from detection | Competent authority | Root cause (if known), containment measures, updated impact |
| Final Report | 1 month from containment | Competent authority | Full root cause analysis, remediation completed, lessons learned |

**Zone C auto-generates the initial notification template** from incident metadata in the audit log.

---

## 5. Business Continuity & Disaster Recovery

### Recovery Time Objectives (RTOs)

| Component | RTO | RPO | Recovery Procedure |
|---|---|---|---|
| Aegis + HSM | 15 min | 0 (no data loss) | Restart container; HSM tokens are volume-mounted |
| Cognitive Core (LLMs) | 30 min | Last checkpoint | Restart LangGraph with checkpoint restore |
| QuestDB | 1 hour | Last WAL segment | Volume restore from snapshot |
| Audit Logger | 4 hours | Last committed row | PostgreSQL point-in-time recovery |
| Full system restart | 2 hours | N/A | Docker Compose up; health checks confirm operational |

### DR Test Schedule

- **Monthly:** Soft Switch + Logic Switch activation and recovery drill
- **Quarterly:** Full system restart drill with RTO measurement
- **Annually:** DORA TLPT (Threat-Led Penetration Testing) — all three zones in scope (whether TLPT applies to this entity and at what frequency requires qualified legal review; none has been performed)

---

## 6. Third-Party ICT Risk Register

| Provider | Service | Criticality | Contract Clause | Last Review |
|---|---|---|---|---|
| AWS (Bedrock) | LLM inference (route open, ADR-003) | High | UNVERIFIED as written: data processing agreement, no model training on customer data | TODO(owner): date |
| AWS (CloudHSM) | Key custody (prod; not provisioned) | Critical | UNVERIFIED as written: shared responsibility model; FIPS 140-2 Level 3 certification | TODO(owner): date |
| Anthropic | LLM model (via Bedrock) | High | UNVERIFIED as written: "GPAI obligations confirmed (EU AI Act)"; the EU AI Act disclosure shows the related requests as unchecked | TODO(owner): date |
| Mistral AI | LLM model (direct API per register TP-004; ADR-002 says via Bedrock: conflict) | High | UNVERIFIED as written: commercial fintech license; EU data residency | TODO(owner): date |
| Polygon.io | Market data | Critical | UNVERIFIED as written: data license; redistribution restrictions documented | TODO(owner): date |
| Alpaca Markets | Execution broker | Critical | UNVERIFIED as written: broker-dealer agreement; API SLA | TODO(owner): date |

---

## 7. Key Rotation Log

| Key ID | Created | Rotated | Reason | Logged to Zone C |
|---|---|---|---|---|
| afe-key-001 | TODO(owner): date (no key has been provisioned; this row is an example, not a record) | — | Initial provisioning | No (no Zone C audit store exists) |

_Keys rotated every 90 days. Emergency rotation within 4h of suspected compromise._

---

## 8. Document Control

| Version | Date | Author | Change |
|---|---|---|---|
| 1.0 | 2026-05-14 | Ali Jendoubi | Initial document |
