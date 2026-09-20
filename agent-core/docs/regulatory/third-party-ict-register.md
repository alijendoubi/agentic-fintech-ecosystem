# DORA ICT Third-Party Register

**Article reference:** DORA Article 28(3)

> **Status: DRAFT / UNVERIFIED register. Requires qualified legal review.**
> No contract, DPA, licence or SLA is stored in this repository. Every contractual, certification, data-residency and compliance-clause cell below (for example "no model training on customer data", "EU (Frankfurt)", "SLA 99.99%", "FINRA member", "commercial fintech license", "GPAI obligations") is an **unverified assertion carried over from the initial draft**, not a checked fact. `Last Reviewed` has never been filled: every row is **TODO(owner): date** until someone reads the actual contract. Whether this register satisfies DORA Article 28 as cited requires qualified legal review (citation unverified).

## Claims vs implementation

| Claim | Evidence in repo? | Note |
|---|---|---|
| All providers registered and reviewed | Rows exist; no review has been done | TODO(owner) per row |
| TP-003 Anthropic "via AWS Bedrock" | No: `llm_clients.py` uses the direct Anthropic SDK; compose injects `ANTHROPIC_API_KEY` | ADR-003 (Proposed) |
| TP-004 Mistral direct commercial API | Consistent with code, but ADR-002 says all models go via Bedrock | ADR-003 (Proposed) |
| TP-002 CloudHSM in use | No: no production HSM exists | Phase 5 §6 |
| TP-007 Alpaca as broker | Working choice for paper trading; production broker undecided (DORA doc) | Phase 5 go/no-go item 2 |
| Failover procedures documented for Critical providers (Notes below) | Only DRAFT `docs/runbooks/dr-failover.md`; no second provider exists | TODO(owner) |
| Register reviewed quarterly | Never reviewed | TODO(owner) |
| Other providers used but not registered | `.env.example` also lists `FMP_API_KEY` (Financial Modeling Prep); it has no row here | TODO(owner): confirm whether used |

All providers below are **candidate/working choices** unless a contract exists (TODO(owner)). All third-party ICT service providers used by the Agentic Fintech Ecosystem must be registered here, updated whenever a provider is added or a contract changes.

| ID | Provider | Registered Name | Country | Service Description | Criticality | Contract Type | Data Residency | Compliance Clauses | Last Reviewed |
|---|---|---|---|---|---|---|---|---|---|
| TP-001 | AWS (Bedrock) | Amazon Web Services EMEA SARL | Luxembourg | LLM inference for Cognitive Core nodes (Claude models) | High | Data Processing Agreement | EU (Frankfurt) | DPA; no model training on customer data; GDPR Art.28 | TODO(owner): date |
| TP-002 | AWS (CloudHSM) | Amazon Web Services EMEA SARL | Luxembourg | FIPS 140-2 Level 3 HSM for API key custody (production) | Critical | AWS Shared Responsibility Model | EU (Frankfurt) | Shared responsibility; CloudHSM SLA 99.99% | TODO(owner): date |
| TP-003 | Anthropic | Anthropic PBC | USA | Claude claude-sonnet-4-6 / Haiku 4.5 models accessed via AWS Bedrock | High | Bedrock pass-through | EU (via Bedrock) | GPAI obligations; no retention of prompts for training | TODO(owner): date |
| TP-004 | Mistral AI | Mistral AI SAS | France (EU) | Mistral Large 2 for Blue/Red debate nodes | High | Commercial API License | EU (Paris) | EU-native; commercial fintech license; data not used for training | TODO(owner): date |
| TP-005 | Polygon.io | Polygon.io Inc. | USA | Level 2 order book, trade prints, fundamentals (US equities) | Critical | Data License Agreement | USA | Data redistribution restrictions; no onward sale of raw data | TODO(owner): date |
| TP-006 | Alpha Vantage | Alpha Vantage Inc. | USA | News sentiment (cold path fallback) | Medium | API License | USA | Standard API terms | TODO(owner): date |
| TP-007 | Alpaca Markets | Alpaca Securities LLC | USA | Commission-free US equities execution broker (paper + live) | Critical | Broker-Dealer Agreement | USA | FINRA member; API SLA; paper trading sandbox available | TODO(owner): date |
| TP-008 | Docker Hub | Docker Inc. | USA | Container base images (postgres, redis, questdb, chroma) | Medium | Docker Business | USA | Terms of Service | TODO(owner): date |

**Notes:**
- Criticality ratings: Critical (trading halts if unavailable), High (degraded operations), Medium (workaround available)
- All Critical providers must have documented failover procedures in the DORA ICT Risk Management Framework
- Register reviewed quarterly and after any new provider onboarding
