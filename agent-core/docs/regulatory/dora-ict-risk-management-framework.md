# DORA ICT Risk Management Framework
## Digital Operational Resilience Act (Regulation 2022/2554)
**Effective since: January 17, 2025**

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
| Zone B Aegis crash | Low | Critical | Dead Man's Switch activates; Hard Switch if unresponsive > 60s |
| HSM token unavailable | Low | Critical | Trading halted immediately; fallback to SoftHSM2 (dev) |
| KL divergence breach | Medium | High | Automated Soft Switch → Logic Switch cascade per thresholds |
| Zone C audit log compromise | Very Low | Critical | Append-only + hash chaining; Zone A/B cannot write to Zone C |
| Exchange connectivity loss | Medium | High | Execution Motor detects ACK timeout; cancel + re-route via SOR |

### 3.2 Concentration Risk

Third-party concentration risks:
- AWS (Bedrock + CloudHSM + VPC): mitigated by multi-region failover plan
- Polygon.io (primary market data): mitigated by Alpha Vantage fallback for cold path
- Alpaca (execution broker): mitigated by paper trading first; production broker TBD

---

## 4. ICT Incident Classification & Reporting

### Classification Criteria

| Category | Definition | Example |
|---|---|---|
| **Major** | Significant disruption to trading operations, regulatory breach, or data integrity compromise | Aegis crash during market hours, audit log chain broken |
| **Minor** | Short disruption (<15min) with no regulatory impact and automatic recovery | Redis restart, QuestDB reconnection |

### Reporting Timeline (Major Incidents)

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
- **Annually:** DORA TLPT (Threat-Led Penetration Testing) — all three zones in scope

---

## 6. Third-Party ICT Risk Register

| Provider | Service | Criticality | Contract Clause | Last Review |
|---|---|---|---|---|
| AWS (Bedrock) | LLM inference | High | Data processing agreement, no model training on customer data | [Date] |
| AWS (CloudHSM) | Key custody (prod) | Critical | Shared responsibility model; FIPS 140-2 Level 3 certification | [Date] |
| Anthropic | LLM model (via Bedrock) | High | GPAI obligations confirmed (EU AI Act) | [Date] |
| Mistral AI | LLM model | High | Commercial fintech license; EU data residency | [Date] |
| Polygon.io | Market data | Critical | Data license; redistribution restrictions documented | [Date] |
| Alpaca Markets | Execution broker | Critical | Broker-dealer agreement; API SLA | [Date] |

---

## 7. Key Rotation Log

| Key ID | Created | Rotated | Reason | Logged to Zone C |
|---|---|---|---|---|
| afe-key-001 | [Date] | — | Initial provisioning | Yes |

_Keys rotated every 90 days. Emergency rotation within 4h of suspected compromise._

---

## 8. Document Control

| Version | Date | Author | Change |
|---|---|---|---|
| 1.0 | 2026-05-14 | Ali Jendoubi | Initial document |
