# Agentic Fintech Ecosystem — Claude Code Guide

## Project Overview
Greenfield autonomous US equities trading system (design + partial implementation). Targets MiFID II RTS 6 + SEC 15c3-5 compliance as an **aim**: nothing here is a verified compliance claim (requires qualified legal review).
Three-zone sovereign architecture: Zone A (Cognitive Core), Zone B (Aegis + Execution), Zone C (Compliance + Audit).

**Docs describe target-state controls unless marked "implemented".** Current verified status is in `agent-core/README.md` ("Current status", updated 2026-09-25). Short version: every package is implemented and unit-tested, including Aegis (C01-C19, kill switches, signing, mTLS), the execution-motor gRPC service and refdata-bridge. The paper-trade signal path (cognitive-core -> Aegis -> execution-motor) is wired (PR #8). The full dev compose stack was started for the first time on 2026-09-25 (ALI-167). It has never run against a real broker, real LLMs or the real Polygon feed, the risk limits are uncalibrated, and the owner decisions in Linear's "Go-Live Readiness Blockers" milestone are open, so the platform is not able to trade for real.

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

# Rust (Aegis needs protoc; sensory-array does not). This Windows machine has no MSVC linker (ALI-18):
# run cargo in the pinned container instead, e.g. from agent-core/:
#   MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd -W):/work" -v afe-cargo-registry:/usr/local/cargo/registry
#     -v afe-aegis-target:/target -e CARGO_TARGET_DIR=/target -w /work/zone-b/aegis rust:1.98-bookworm
#     bash -c 'apt-get update -qq && apt-get install -y -qq protobuf-compiler && cargo test --locked'
#   (one command, split here for reading; MSYS_NO_PATHCONV stops Git Bash rewriting the paths)
cd agent-core/zone-b/sensory-array && cargo test --locked

# HITL operator terminal (Next.js, port 3000, GET /api/health)
cd agent-core/zone-c/hitl-interface && npm ci && npm run typecheck && npm run lint && npm test && npm run build

# Protobuf
cd agent-core/shared/proto && buf lint && BASE_REF=origin/main bash check-breaking.sh
bash agent-core/shared/proto/generate.sh    # Python stubs (needs grpcio-tools)

# Compose (full dev stack first started 2026-09-25, ALI-167). Dev needs the dev-tls bootstrap first:
cd agent-core/infrastructure/dev-tls && sh generate-dev-certs.sh --yes-i-know-this-is-dev-only
cd .. && cp .env.example .env    # fill every blank; secrets are ${VAR:?}
docker compose config
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d   # dev only: host ports, dev mTLS certs, dev signer

# Live-stack integration tests against that running stack (ALI-166, PR #13)
cd agent-core/tests/live && AFE_LIVE_STACK=1 python -m pytest -v

# CI: .github/workflows/ci.yml (fail-loud, per package). Actions is billing-locked, so it has never run on GitHub.
```

## Architecture Quick Reference
Implemented (see the README status table for what was and was not re-run):
- `agent-core/zone-a/cognitive-core/` - LangGraph Blue/Red/Judge debate, runner service (`service.py`) + Dockerfile
- `agent-core/zone-a/regime-detector/` - 5-state HMM; `hmm.py` shim over the `regime_detector/` package; HMAC-verified `.npz` model store
- `agent-core/zone-b/sensory-array/` - Rust WebSocket L2 ingestor, QuestDB ILP + Redis sinks (hot path, no MCP)
- `agent-core/zone-a/vector-db/` - `afe_vector_memory` Chroma memory layer (no seed scenarios yet, ALI-157)
- `agent-core/zone-b/aegis/` - Rust risk gate: controls C01-C19, kill switches, attestation signing, gRPC/mTLS, `aegis supervisor`
- `agent-core/zone-b/execution-motor/` - gRPC service over mTLS: attestation-verified execution, Alpaca paper client, mock broker
- `agent-core/zone-b/refdata-bridge/` - Redis -> `Aegis.PushReferenceData` over mTLS
- `agent-core/zone-c/audit-logger|compliance-manifest|sharp-gate/` - Python libraries (`afe_audit`, `afe_manifest`, `afe_sharp`)
- `agent-core/zone-c/hitl-interface/` - Next.js terminal; the backend it talks to does not exist (`docs/api-contract.md` PROPOSED)
- `agent-core/backtesting/` - engine, WFA, Monte Carlo, calibration machinery; synthetic data only, no calibrated result

All specs are in `agent-core/docs/specs/`; open decisions are in `agent-core/docs/adr/` (ADR-003, ADR-004 are Proposed).

## Conventions
- Intended: Zone A holds no trading/signing/broker credentials — only Aegis authorises orders. Compose no longer injects LLM provider keys (cognitive-core uses Bedrock/IAM, ADR-002); the LLM egress route is still open in ADR-003 (Proposed).
- Hot path is MCP-free — direct socket connections only
- All orders must carry a valid Aegis attestation: execution-motor verifies it before the broker (a separate broker gateway, ADR-004, is not implemented)
- Intended: every trade decision generates a Compliance Manifest in Zone C (library exists; not wired into a running service, ALI-161)
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
