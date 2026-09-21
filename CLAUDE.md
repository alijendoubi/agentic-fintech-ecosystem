# Agentic Fintech Ecosystem — Claude Code Guide

## Project Overview
Greenfield autonomous US equities trading system (design + partial implementation). Targets MiFID II RTS 6 + SEC 15c3-5 compliance as an **aim**: nothing here is a verified compliance claim (requires qualified legal review).
Three-zone sovereign architecture: Zone A (Cognitive Core), Zone B (Aegis + Execution), Zone C (Compliance + Audit).

**Docs describe target-state controls unless marked "implemented".** Current verified status is in `agent-core/README.md` ("Current status", updated 2026-09-21). Short version: cognitive-core, regime-detector, sensory-array, execution-motor (library only), audit-logger, compliance-manifest, sharp-gate, backtesting and the HITL terminal are implemented and unit-tested in isolation; Aegis is still a placeholder in the integration tree (implementation lives on branch `pkg-e/aegis`); nothing is wired end to end and the platform cannot trade.

## Key Commands
```bash
# Lint: repo ruff.toml, run from each package directory (isort first-party detection depends on cwd)
cd agent-core/zone-a/regime-detector && ruff check --config ../../../ruff.toml .

# Python packages
cd agent-core/zone-a/cognitive-core  && python -m pytest
cd agent-core/zone-a/regime-detector && pip install -r requirements-dev.txt && python -m pytest   # hmm.py is only a shim over regime_detector/
cd agent-core/zone-b/execution-motor && pip install -r requirements-dev.txt && python -m pytest && python -m mypy .
cd agent-core/zone-c/audit-logger    && python -m pytest        # integration tests start postgres:16-alpine via Docker

# Backtesting (run from agent-core/)
cd agent-core && python -m pytest backtesting -c backtesting/pytest.ini && python -m mypy --config-file backtesting/mypy.ini backtesting
python -m backtesting.engine --help   # smoke CLI; python -m backtesting.calibrate_ptc needs a real dataset

# Sensory array (Rust; NO protoc needed)
cd agent-core/zone-b/sensory-array && cargo test --locked

# HITL operator terminal (Next.js, port 3000, GET /api/health)
cd agent-core/zone-c/hitl-interface && npm ci && npm run typecheck && npm run lint && npm test && npm run build

# Protobuf
cd agent-core/shared/proto && buf lint && BASE_REF=origin/main bash check-breaking.sh
bash agent-core/shared/proto/generate.sh    # Python stubs (needs grpcio-tools)

# Compose (validated with `docker compose config`; the full stack has NOT been started)
cd agent-core/infrastructure && cp .env.example .env    # fill every blank; secrets are ${VAR:?}
docker compose config
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d   # dev only: host ports, plaintext Aegis

# CI: .github/workflows/ci.yml (fail-loud, per package). Actions is billing-locked, so it has never run on GitHub.
```

## Architecture Quick Reference
Implemented (see the README status table for what was and was not re-run):
- `agent-core/zone-a/cognitive-core/` - LangGraph Blue/Red/Judge debate (runner service + Dockerfile on branch `pkg-a2/vector-runner`)
- `agent-core/zone-a/regime-detector/` - 5-state HMM; `hmm.py` shim over the `regime_detector/` package; HMAC-verified `.npz` model store
- `agent-core/zone-b/sensory-array/` - Rust WebSocket L2 ingestor, QuestDB ILP + Redis sinks (hot path, no MCP)
- `agent-core/zone-b/execution-motor/` - Python library (no Dockerfile/entrypoint/service yet)
- `agent-core/zone-c/audit-logger|compliance-manifest|sharp-gate/` - Python libraries (`afe_audit`, `afe_manifest`, `afe_sharp`)
- `agent-core/zone-c/hitl-interface/` - Next.js terminal; the backend it talks to does not exist (`docs/api-contract.md` PROPOSED)
- `agent-core/backtesting/` - engine, WFA, Monte Carlo, calibration machinery; synthetic data only, no calibrated result

Not in the integration tree yet: `zone-b/aegis` (implementation on `pkg-e/aegis`; placeholder `main.rs` here), `zone-a/vector-db` memory layer.

All specs are in `agent-core/docs/specs/`; open decisions are in `agent-core/docs/adr/` (ADR-003, ADR-004 are Proposed).

## Conventions
- Intended: Zone A holds no trading/signing/broker credentials — only Aegis authorises orders. Compose no longer injects LLM provider keys (cognitive-core uses Bedrock/IAM, ADR-002); the LLM egress route is still open in ADR-003 (Proposed).
- Hot path is MCP-free — direct socket connections only
- Intended: all orders pass Aegis PTCs before reaching the broker (Aegis not in this tree; broker gateway not implemented)
- Intended: every trade decision generates a Compliance Manifest in Zone C (library exists; not wired into a running service)
- Compose/CI: secrets only via `${VAR:?}` env (`agent-core/infrastructure/.env.example` has blanks); zone networks are `internal: true`; `*.sh`/`*.sql`/Dockerfiles must be LF (`.gitattributes`)
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
