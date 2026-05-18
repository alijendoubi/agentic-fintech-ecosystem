# DORA ICT Third-Party Register

**Article reference:** DORA Article 28(3)

All third-party ICT service providers used by the Agentic Fintech Ecosystem must be registered here. Updated whenever a new provider is added or an existing contract changes.

| ID | Provider | Registered Name | Country | Service Description | Criticality | Contract Type | Data Residency | Compliance Clauses | Last Reviewed |
|---|---|---|---|---|---|---|---|---|---|
| TP-001 | AWS (Bedrock) | Amazon Web Services EMEA SARL | Luxembourg | LLM inference for Cognitive Core nodes (Claude models) | High | Data Processing Agreement | EU (Frankfurt) | DPA; no model training on customer data; GDPR Art.28 | [Date] |
| TP-002 | AWS (CloudHSM) | Amazon Web Services EMEA SARL | Luxembourg | FIPS 140-2 Level 3 HSM for API key custody (production) | Critical | AWS Shared Responsibility Model | EU (Frankfurt) | Shared responsibility; CloudHSM SLA 99.99% | [Date] |
| TP-003 | Anthropic | Anthropic PBC | USA | Claude claude-sonnet-4-6 / Haiku 4.5 models accessed via AWS Bedrock | High | Bedrock pass-through | EU (via Bedrock) | GPAI obligations; no retention of prompts for training | [Date] |
| TP-004 | Mistral AI | Mistral AI SAS | France (EU) | Mistral Large 2 for Blue/Red debate nodes | High | Commercial API License | EU (Paris) | EU-native; commercial fintech license; data not used for training | [Date] |
| TP-005 | Polygon.io | Polygon.io Inc. | USA | Level 2 order book, trade prints, fundamentals (US equities) | Critical | Data License Agreement | USA | Data redistribution restrictions; no onward sale of raw data | [Date] |
| TP-006 | Alpha Vantage | Alpha Vantage Inc. | USA | News sentiment (cold path fallback) | Medium | API License | USA | Standard API terms | [Date] |
| TP-007 | Alpaca Markets | Alpaca Securities LLC | USA | Commission-free US equities execution broker (paper + live) | Critical | Broker-Dealer Agreement | USA | FINRA member; API SLA; paper trading sandbox available | [Date] |
| TP-008 | Docker Hub | Docker Inc. | USA | Container base images (postgres, redis, questdb, chroma) | Medium | Docker Business | USA | Terms of Service | [Date] |

**Notes:**
- Criticality ratings: Critical (trading halts if unavailable), High (degraded operations), Medium (workaround available)
- All Critical providers must have documented failover procedures in the DORA ICT Risk Management Framework
- Register reviewed quarterly and after any new provider onboarding
