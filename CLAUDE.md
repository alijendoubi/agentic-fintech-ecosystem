# Agentic Fintech Ecosystem — Claude Code Guide

## Project Overview
Greenfield autonomous US equities trading system (design + partial implementation). Targets MiFID II RTS 6 + SEC 15c3-5 compliance as an **aim**: nothing here is a verified compliance claim (requires qualified legal review).
Three-zone sovereign architecture: Zone A (Cognitive Core), Zone B (Aegis + Execution), Zone C (Compliance + Audit).

**Docs describe target-state controls unless marked "implemented".** Current verified status is in `agent-core/README.md` ("Current status"). As of 2026-09-19 Aegis is a placeholder and execution-motor, audit-logger, compliance-manifest and backtesting are empty packages; `zone-c/hitl-interface` does not exist.

## Key Commands
```bash
# Cognitive Core unit tests (implemented)
cd agent-core/zone-a/cognitive-core && python -m pytest

# Sensory array (Rust, implemented; needs protoc)
cd agent-core/zone-b/sensory-array && cargo test

# Generate Python proto stubs (needs grpcio-tools)
bash agent-core/shared/proto/generate.sh

# Full stack (target only: most Dockerfiles do not exist yet)
docker-compose -f agent-core/infrastructure/docker-compose.yml up

# PLANNED, not runnable today:
#   Aegis (Rust): agent-core/zone-b/aegis        (placeholder main.rs; spec: docs/specs/phase_3_aegis_execution.md)
#   HITL terminal: agent-core/zone-c/hitl-interface (does not exist; spec: docs/specs/phase_4_backtesting_compliance.md §10)
#   Backtesting:   agent-core/backtesting          (empty package; spec: docs/specs/phase_4_backtesting_compliance.md)
```

## Architecture Quick Reference
Implemented:
- `agent-core/zone-a/cognitive-core/graph.py` — LangGraph Blue/Red/Judge debate
- `agent-core/zone-a/regime-detector/hmm.py` — 5-state HMM regime classifier
- `agent-core/zone-b/sensory-array/src/ingestor.rs` — Rust WebSocket L2 ingestor (hot path, no MCP)

Planned (files below do NOT exist; each is planned by the named spec):
- `zone-b/aegis/src/` PTC engine, kill switches, signing — Phase 3 spec (the earlier reference to `aegis/src/ptc.rs` is a placeholder name only)
- `zone-b/execution-motor/` execution + broker gateway — Phase 3 spec §10 (the earlier `sor.py` does not exist; SOR is deferred)
- `zone-c/compliance-manifest/` per-trade manifest — Phase 4 spec §9 (no `manifest.py`)
- `backtesting/` engine, regime-aware WFA, Monte Carlo — Phase 4 spec §4-§6 (no `engine.py` / `wfa.py`)
- `zone-c/hitl-interface/` — Phase 4 spec §10

All specs are in `agent-core/docs/specs/`; open decisions are in `agent-core/docs/adr/` (ADR-003, ADR-004 are Proposed).

## Conventions
- Intended: Zone A holds no trading/signing/broker credentials — only Aegis authorises orders. **Currently violated for LLM keys** (compose injects `MISTRAL_API_KEY`/`ANTHROPIC_API_KEY`); see ADR-003 (Proposed).
- Hot path is MCP-free — direct socket connections only
- Intended: all orders pass Aegis PTCs before reaching the broker (not implemented)
- Intended: every trade decision generates a Compliance Manifest in Zone C (not implemented)
- No floating point for money on the decision path (Phase 3 spec §3)
- Latency budgets are unvalidated targets and inconsistent between README, ADR-002 and code; see README "Latency Budget"
- Never state a regulatory or calibration result without evidence; use `TODO(owner): ...` for unknown facts and label proposals PROPOSED
- The design blueprint `agentic_fintech_blueprint_v2.md` is an **external** document (Obsidian/notes), not in this repo; do not cite "blueprint Section 3" as if it were resolvable

<!-- unified-memory start -->
## Memory
This project uses [unified-memory](https://github.com/alijendoubi/unified-memory) for persistent session context.
- Say **"pick up where we left off"** at the start of each session
- Say **"wrap up"** at session end to save progress
- Run `umem doctor` to audit memory health
- Run `umem compact` to reduce token usage
<!-- unified-memory end -->
