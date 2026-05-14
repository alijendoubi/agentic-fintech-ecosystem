# agent-core — Agentic Fintech Ecosystem

Greenfield autonomous US equities trading system. Replaces `Trading-News/` as the primary project.

## Architecture: Three-Zone Sovereign Runtime

```
Zone A — Thinking (GPU, LLMs, LangGraph, HMM, VectorDB)
  zone-a/cognitive-core/     Python: LangGraph Blue/Red/Judge/Reflector debate
  zone-a/regime-detector/    Python: HMM 5-state market regime classifier
  zone-a/vector-db/          ChromaDB: adversarial scenario embeddings

Zone B — Execution (Aegis, SOR, HSM, FIX)
  zone-b/sensory-array/      Rust: WebSocket L2 ingestor, QuestDB writer (hot path)
  zone-b/aegis/              Rust: Deterministic PTCs, HSM signing, heartbeat
  zone-b/execution-motor/    Python: Smart Order Router, Alpaca FIX client

Zone C — Compliance (audit logs, HITL terminal, DORA reporting)
  zone-c/audit-logger/       Python: append-only + hash-chained audit logs, KL monitor
  zone-c/compliance-manifest/ Python: per-trade manifest generation pipeline
  zone-c/hitl-interface/     Next.js: operator HITL terminal

Shared
  shared/proto/              Protobuf definitions for inter-zone gRPC contracts
  backtesting/               Python: event-driven sim, Monte Carlo, regime-aware WFA
  infrastructure/            Docker Compose, network policies, .env.example
  docs/                      ADRs, regulatory docs (MiFID II, DORA, EU AI Act), processes
```

## Quick Start

```bash
# Copy and configure environment
cp agent-core/infrastructure/.env.example agent-core/infrastructure/.env

# Start all zones
docker-compose -f agent-core/infrastructure/docker-compose.yml up

# Generate protobuf stubs
bash agent-core/shared/proto/generate.sh
```

## Latency Budget (End-to-End: < 2,600ms)

| Stage | Budget |
|---|---|
| Sensory Array → Cognitive Core handoff | < 500ms |
| Blue + Red debate | < 1,500ms |
| Judge synthesis | < 500ms |
| Aegis PTC validation | < 50ms |
| Execution Motor → Exchange ACK | < 10ms |

## Key Design Principles

- Zone A holds **zero** API keys — only Aegis (Zone B) signs
- Hot path is **MCP-free** — direct socket connections only
- All orders pass Aegis PTCs before reaching the exchange
- Every trade decision generates a Compliance Manifest (Zone C, 7-year retention)
- KL divergence monitoring detects distributional shift — automated circuit breakers

## Regulatory Compliance

- **MiFID II RTS 6** (primary): PTCs, kill switches, annual self-assessment — see `docs/regulatory/`
- **DORA** (live Jan 2025): ICT risk management framework — see `docs/regulatory/dora-ict-risk-management-framework.md`
- **EU AI Act** (Aug 2026): Limited Risk classification — see `docs/regulatory/eu-ai-act-limited-risk-disclosure.md`
- **SEC 15c3-5**: US market access rule — PTCs satisfy this via Aegis layer

## Implementation Phases

| Phase | Status | Branch |
|---|---|---|
| Step 0: unified-memory | ✅ Complete | `feat/unified-memory-install` |
| Phase 0: Foundation scaffold | 🔄 In Progress | `feat/phase-0-foundation` |
| Phase 1: Sensory Array | ⏳ Pending | `feat/phase-1-sensory-array` |
| Phase 2: Cognitive Core | ⏳ Pending | `feat/phase-2-cognitive-core` |
| Phase 3: Aegis + Execution | ⏳ Pending | `feat/phase-3-aegis-execution` |
| Phase 4: Backtesting + Compliance | ⏳ Pending | `feat/phase-4-backtesting-compliance` |
| Phase 5: Canary + Production | ⏳ Pending | `feat/phase-5-canary-production` |
