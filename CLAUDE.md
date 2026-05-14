# Agentic Fintech Ecosystem — Claude Code Guide

## Project Overview
Greenfield autonomous US equities trading system. Targets MiFID II RTS 6 + SEC 15c3-5 compliance.
Three-zone sovereign architecture: Zone A (Cognitive Core), Zone B (Aegis + Execution), Zone C (Compliance + Audit).

## Key Commands
```bash
# Start the full stack
docker-compose -f agent-core/infrastructure/docker-compose.yml up

# Zone A — Cognitive Core (Python)
cd agent-core/zone-a && python -m pytest

# Zone B — Aegis / Sensory Array (Rust)
cd agent-core/zone-b/aegis && cargo test
cd agent-core/zone-b/sensory-array && cargo test

# Zone C — HITL terminal (Next.js)
cd agent-core/zone-c/hitl-interface && yarn dev

# Backtesting
cd agent-core/backtesting && python engine.py --mode paper
```

## Architecture Quick Reference
- `zone-a/cognitive-core/graph.py` — LangGraph Blue/Red/Judge debate
- `zone-a/regime-detector/hmm.py` — 5-state HMM regime classifier
- `zone-b/sensory-array/src/ingestor.rs` — Rust WebSocket L2 ingestor (hot path, no MCP)
- `zone-b/aegis/src/ptc.rs` — Deterministic pre-trade controls (hard + soft blocks)
- `zone-b/execution-motor/sor.py` — Smart Order Router with venue toxicity scoring
- `zone-c/compliance-manifest/manifest.py` — Per-trade manifest with hash chaining
- `backtesting/engine.py` — Event-driven backtesting with regime-aware WFA

## Conventions
- Cognitive Core (Zone A) holds **zero** API keys — only Aegis (Zone B) signs
- Hot path is MCP-free — direct socket connections only
- All orders must pass Aegis PTCs before reaching the exchange
- Every trade decision generates a Compliance Manifest written to Zone C
- Latency budget: end-to-end < 2,600ms (see blueprint Section 3)

<!-- unified-memory start -->
## Memory
This project uses [unified-memory](https://github.com/alijendoubi/unified-memory) for persistent session context.
- Say **"pick up where we left off"** at the start of each session
- Say **"wrap up"** at session end to save progress
- Run `umem doctor` to audit memory health
- Run `umem compact` to reduce token usage
<!-- unified-memory end -->
