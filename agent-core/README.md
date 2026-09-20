# agent-core — Agentic Fintech Ecosystem

Greenfield autonomous US equities trading system (design and partial implementation). Replaces `Trading-News/` as the primary project.

> **Read this first: documentation describes TARGET-STATE controls unless a control is explicitly marked "implemented".**
> As of 2026-09-19 the platform is **not** capable of trading, paper or live: the pre-trade control component (Aegis), execution motor, audit logger, compliance manifest, backtesting engine and HITL interface are not implemented. Regulatory documents under `docs/regulatory/` are drafts/templates that require qualified legal review; they are not evidence of compliance. Whether to ever go live is an owner decision (`docs/specs/phase_5_canary_production.md`).

## Current status (as of 2026-09-19)

Verified against the source tree on that date. "Implemented" means code exists in the repo; it does not mean validated in production or against a real broker.

| Area | State | Evidence in tree |
|---|---|---|
| `zone-a/cognitive-core` (LangGraph Blue/Red/Judge/Compression/Reflector) | **Implemented (unit-tested with mocked LLMs)**. 11 tests passed in a local run on 2026-09-19. Not connected to Aegis (no gRPC client); no Dockerfile; LLM access route unresolved (ADR-003) | `graph.py`, `models.py`, `prompts.py`, `reflector.py`, `llm_clients.py`, `tests/` |
| `zone-a/regime-detector` (HMM, 5 states) | **Implemented, no tests.** Single module `hmm.py`; Dockerfile exists | `hmm.py`, `Dockerfile` |
| `zone-a/vector-db` | **Empty package** (`__init__.py` only). Compose uses the `chromadb/chroma` image; no scenario data | `__init__.py` |
| `zone-b/sensory-array` (Rust ingestor, normalizer, QuestDB writer) | **Implemented; unit tests exist in `normalizer.rs`, `ttl.rs`, `questdb_writer.rs`.** Build not verified in this pass (a local `cargo check --offline` failed while compiling dependency build scripts; not diagnosed; CI result not checked). Never run against Polygon | `src/*.rs`, `Dockerfile`, `build.rs` |
| `zone-b/aegis` | **Placeholder only**: `main.rs` prints a message. No controls, no kill switches, no HSM code | `src/main.rs` (3 lines), `Cargo.toml` |
| `zone-b/execution-motor` | **Empty package** (`__init__.py`, `requirements.txt`) | — |
| `zone-c/audit-logger`, `zone-c/compliance-manifest` | **Empty packages** | — |
| `zone-c/hitl-interface` | **Does not exist** (referenced by compose and CI) | — |
| `backtesting/` | **Empty package** (no engine, WFA or Monte Carlo) | `__init__.py`, `requirements.txt` |
| `shared/proto` | **Messages only** (4 files); no `service`/`rpc`; money as `double` | `*.proto`, `generate.sh` |
| `infrastructure/docker-compose.yml` | Present but **cannot start the full stack**: Dockerfiles exist only for `regime-detector` and `sensory-array`; several network/secret inconsistencies are documented in `docs/specs/phase_3_aegis_execution.md` §14 and ADR-003 | `docker-compose.yml` |
| Regulatory documents | **Drafts/templates**; several stated results are unsubstantiated (`ptc-calibration.md` banner) | `docs/regulatory/` |
| Runbooks | **DRAFT, untested; no drill ever run** | `docs/runbooks/` |

## Architecture: Three-Zone Sovereign Runtime (target design)

Directories marked **(planned)** are empty or missing today; see the spec that plans them.

```
Zone A — Thinking (GPU, LLMs, LangGraph, HMM, VectorDB)
  zone-a/cognitive-core/      Python: LangGraph Blue/Red/Judge/Reflector debate (implemented)
  zone-a/regime-detector/     Python: HMM 5-state market regime classifier (implemented)
  zone-a/vector-db/           ChromaDB: adversarial scenario embeddings (planned; empty package)

Zone B — Execution (Aegis, SOR, HSM, FIX)
  zone-b/sensory-array/       Rust: WebSocket L2 ingestor, QuestDB writer (implemented, hot path)
  zone-b/aegis/               Rust: deterministic PTCs, HSM signing, kill switches (planned: docs/specs/phase_3_aegis_execution.md; placeholder only)
  zone-b/execution-motor/     Python: order execution; Smart Order Router deferred (planned: Phase 3 spec §10; empty package)

Zone C — Compliance (audit logs, HITL terminal, DORA reporting)
  zone-c/audit-logger/        Python: append-only, hash-chained audit logs, KL monitor (planned: docs/specs/phase_4_backtesting_compliance.md §8; empty package)
  zone-c/compliance-manifest/ Python: per-trade manifest pipeline (planned: Phase 4 spec §9; empty package)
  zone-c/hitl-interface/      Next.js: operator HITL terminal (planned: Phase 4 spec §10; directory does not exist)

Shared
  shared/proto/               Protobuf message definitions; Aegis service definition proposed in Phase 3 spec Appendix A
  backtesting/                Python: event-driven sim, Monte Carlo, regime-aware WFA (planned: Phase 4 spec §4-§6; empty package)
  infrastructure/             Docker Compose, .env.example
  docs/                       ADRs, specs, regulatory drafts, runbooks, processes
```

## Quick Start

The full stack does **not** currently start (missing Dockerfiles for most services). What works today is per-package development:

```bash
# Cognitive core unit tests (Python 3.12)
cd agent-core/zone-a/cognitive-core && python -m pytest

# Sensory array (Rust; needs protoc for tonic-build)
cd agent-core/zone-b/sensory-array && cargo test

# Generate Python protobuf stubs (needs: pip install grpcio-tools)
bash agent-core/shared/proto/generate.sh

# Environment template (never commit .env)
cp agent-core/infrastructure/.env.example agent-core/infrastructure/.env
```

Target (once Dockerfiles exist): `docker-compose -f agent-core/infrastructure/docker-compose.yml up`.

## Latency Budget (targets; none has been measured or validated)

| Stage | Budget in earlier README | Note |
|---|---|---|
| Sensory Array -> Cognitive Core handoff | < 500 ms | unmeasured |
| Blue + Red debate | < 1,500 ms | ADR-002 and `graph.py` allot 800 + 800 = 1,600 ms sequentially: **inconsistent** |
| Judge synthesis | < 500 ms | matches ADR-002 |
| Compression | (not listed) | ADR-002 allots 200 ms |
| Aegis PTC validation | < 50 ms | target; see Phase 3 spec §8; unmeasured (Aegis does not exist) |
| Execution Motor -> Exchange ACK | < 10 ms | no evidence this is achievable with the working broker; unvalidated |

The earlier stated end-to-end total of < 2,600 ms is not consistent with the ADR-002 per-node budgets (they sum to 2,300 ms for the debate alone). TODO(owner): decide the real end-to-end budget.

## Design principles (intent; not all enforced today)

- **Aegis is the only component that may authorise an order** (target). Zone A must hold no trading/signing/broker credentials. **Known conflict:** `docker-compose.yml` injects LLM provider keys (`MISTRAL_API_KEY`, `ANTHROPIC_API_KEY`) into Zone A and the code uses the providers' direct SDKs, so "Zone A holds zero API keys" is **not currently true** for LLM keys. See `docs/adr/ADR-003-zone-a-llm-access.md` (Proposed).
- Hot path is MCP-free — direct socket connections only.
- All orders must pass Aegis PTCs before reaching the broker (target; not implemented).
- Every trade decision should generate a Compliance Manifest (Zone C, 7-year retention; requirement basis requires qualified legal review) (target; not implemented).
- KL divergence monitoring to trigger circuit breakers (target; reference distribution undefined).
- Fail closed everywhere (Phase 3 spec §9).

## Regulatory context (requires qualified legal review; nothing here is a compliance claim)

- **MiFID II RTS 6**: pre-trade controls, kill switches, annual self-assessment: `docs/regulatory/mifid-ii-rts6-self-assessment-template.md` (template, unassessed).
- **DORA**: ICT risk management framework draft: `docs/regulatory/dora-ict-risk-management-framework.md`.
- **EU AI Act**: classification working assumption and disclosure draft: `docs/regulatory/eu-ai-act-limited-risk-disclosure.md`. The application date cited there (2 Aug 2026) has passed and is unverified.
- **US market-access rules (SEC 15c3-5)**: an earlier version of this README said the PTCs "satisfy" this rule. That is a legal conclusion that has not been verified; which entity the rule binds and whether Aegis controls could satisfy it require qualified legal review.
- Whether any of these regimes apply to the operating entity, and whether authorisation/registration is needed, is unanswered: TODO(owner).

## Implementation phases

| Phase | Status (2026-09-19) | Spec | Notes |
|---|---|---|---|
| Step 0: unified-memory | Done (tooling) | n/a | Claude Code memory plugin; unrelated to trading logic |
| Phase 0: Foundation scaffold | **Done** (merged): directory scaffold, compose, protos, docs, CI | n/a | Compose/proto/CI gaps remain (see status table) |
| Phase 1: Sensory Array + Regime Detector | **Code implemented; build/tests not verified in this pass; regime-detector untested** | `docs/specs/phase_1_sensory_array.md` | Spec module map for regime-detector (`questdb_client.py`, `redis_client.py`) does not match the single-file implementation |
| Phase 2: Cognitive Core | **Code implemented; 11 unit tests pass (mocked LLMs)**; not integrated | `docs/specs/phase_2_cognitive_core.md` | LLM access unresolved (ADR-003) |
| Phase 3: Aegis + Execution | **Not started (placeholder).** Spec drafted | `docs/specs/phase_3_aegis_execution.md` | Includes proto snippets for the Aegis service |
| Phase 4: Backtesting + Compliance | **Not started (empty packages).** Spec drafted | `docs/specs/phase_4_backtesting_compliance.md` | Needed before any threshold can be called calibrated |
| Phase 5: Canary + Production | **Not started.** Spec drafted; go-live is an owner decision | `docs/specs/phase_5_canary_production.md` | |

## Documentation index

- Specs: `docs/specs/` (phases 1-5)
- Decisions: `docs/adr/` (ADR-001, ADR-002 Accepted with status notes on open conflicts; ADR-003, ADR-004 **Proposed**)
- Process: `docs/processes/sharp-promotion.md`
- Regulatory drafts: `docs/regulatory/`
- Runbooks (DRAFT, untested): `docs/runbooks/`
- The original design "blueprint" (`agentic_fintech_blueprint_v2.md`) is **not in this repository**; it was an external document read in the initial session (see root `MEMORY.md`). References to a "blueprint Section 3" cannot be resolved from the repo.
