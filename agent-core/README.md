# agent-core — Agentic Fintech Ecosystem

Greenfield autonomous US equities trading system (design and partial implementation). Replaces `Trading-News/` as the primary project.

> **Read this first: documentation describes TARGET-STATE controls unless a control is explicitly marked "implemented".**
> The platform is **not** capable of trading, paper or live. Even where a component below is implemented and unit-tested, the
> end-to-end path is not wired: nothing feeds Aegis reference data, cognitive-core cannot authenticate to a production-mode
> Aegis (plaintext gRPC channel), there is no HITL backend, and the execution motor has no service entrypoint. Regulatory
> documents under `docs/regulatory/` are drafts/templates that require qualified legal review; they are not evidence of
> compliance. Whether to ever go live is an owner decision (`docs/specs/phase_5_canary_production.md`).

## Current status (integration tree, updated 2026-09-21)

"Implemented" means code exists in the repo; it does not mean validated in production or against a real broker/feed.
"Ran" means PKG-D2 executed the command in a local worktree on 2026-09-21; anything else is taken from the package's own
README/tests and was **not** re-run in that pass. Packages still landing from other branches (Aegis, vector memory and the
cognitive-core runner/Dockerfile) are listed separately below.

| Area | State | Evidence in tree |
|---|---|---|
| `zone-a/cognitive-core` (LangGraph Blue/Red/Judge/Compression/Reflector) | **Implemented, unit tests with mocked LLMs** (not re-run in the PKG-D2 pass: dependency install failed on a flaky network). LLM access route unresolved (ADR-003 Proposed; code uses Bedrock/IAM). Runner service + Dockerfile are on branch `pkg-a2/vector-runner`, not in this tree | `graph.py`, `models.py`, `prompts.py`, `reflector.py`, `llm_clients.py`, `proto_mapping.py`, `tests/` |
| `zone-a/regime-detector` (HMM, 5 states) | **Implemented and tested.** `hmm.py` is a thin entrypoint shim over the `regime_detector/` package. Ran: `python -m pytest` = 134 passed. Models persist as HMAC-verified `.npz` (not pickle) and are only saved/loaded when `MODEL_HMAC_KEY` is set. Dockerfile exists; image build not run by PKG-D2 | `hmm.py`, `regime_detector/`, `tests/`, `Dockerfile` |
| `zone-a/vector-db` | **Empty package in this tree** (`__init__.py` only). `afe_vector_memory` (Chroma memory layer) lands from `pkg-a2/vector-runner`. Compose runs the pinned `chromadb/chroma:1.5.9` image | `__init__.py` |
| `zone-b/sensory-array` (Rust ingestor, normalizer, ILP/Redis sinks, health) | **Implemented.** No `protoc` or protobuf codegen needed (self-contained crate). Unit + integration tests exist (`tests/ingestor_reconnect.rs`, `tests/live_sinks.rs`). `cargo fmt --check` ran clean; clippy/test results: see PKG-D2 report (crates.io was unreachable at times). Never run against Polygon | `src/*.rs`, `Cargo.lock`, `Dockerfile` |
| `zone-b/aegis` | **Placeholder in this tree** (`main.rs` prints a message). The implementation (controls C01-C19, kill switches, attestation signing, gRPC over mTLS, Dockerfile, README with env vars) lands from branch `pkg-e/aegis` | `src/main.rs`, `Cargo.toml` |
| `zone-b/execution-motor` | **Library, no service.** Models, limits, halt, attestation checks, Alpaca client, proto adapter; no Dockerfile, no entrypoint, no gRPC server. Ran: `python -m pytest` = 259 passed; `python -m mypy .` clean | `execution_motor/`, `tests/` |
| `zone-c/audit-logger` | **Implemented (library `afe_audit`)**: append-only hash-chained audit trail on PostgreSQL 16, verifier, anchors, KL drift functions. Integration tests use Docker and were **not re-run** by PKG-D2. Compose mounts its `sql/` into `postgres-audit` (verified: init creates the two roles and schema; see `infrastructure/`) | `afe_audit/`, `sql/`, `README.md` |
| `zone-c/compliance-manifest`, `zone-c/sharp-gate` | **Implemented (libraries `afe_manifest`, `afe_sharp`)**; not re-run by PKG-D2 | `README.md` in each |
| `zone-c/hitl-interface` | **Implemented (Next.js 16, port 3000, `/api/health`), tested in isolation.** Ran: `npm ci`, `npm run typecheck`, `npm run lint`, `npm test` (177 passed; one earlier run under heavy machine load reported a worker error, the rerun was clean). `npm run build` not run by PKG-D2. No HITL backend exists (`docs/api-contract.md` is PROPOSED); every decision fails closed | `package.json`, `README.md` |
| `backtesting/` | **Implemented, synthetic data only.** Engine, WFA, Monte Carlo, calibration machinery. Ran: `pytest` = 111 passed, `ruff` and `mypy` clean. Produces no calibrated result (no real dataset in the repo) | `README.md` |
| `shared/proto` | Messages plus the `Aegis` service (`aegis.proto`) and `compliance_manifest.proto`; `buf lint` and `check-breaking.sh` pass locally (Ran); money as `_nanos` int64 fields alongside legacy doubles | `*.proto`, `buf.yaml`, `generate.sh`, `check-breaking.sh` |
| `infrastructure/docker-compose.yml` | Reconciled with each package's Dockerfile, env names, ports and health checks; secrets are `${VAR:?}`; zone networks internal; only HITL publishes a host port (127.0.0.1). `docker compose config` passes; `vector-db`, `redis`, `questdb`, `postgres-audit` were started and became healthy (Ran). The **full stack has not been started**. `execution-motor` is behind profile `pending-impl` (nothing to run) | `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example` |
| CI (`.github/workflows/ci.yml`) | Fail-loud per-package jobs (ruff, pytest, mypy, cargo fmt/clippy/test, HITL, buf, docker build smoke, compose validation). **Never run on GitHub: Actions is billing-locked.** Commands were validated locally where noted in the PKG-D2 report | `.github/workflows/ci.yml` |
| Regulatory documents | **Drafts/templates**; several stated results are unsubstantiated (`ptc-calibration.md` banner) | `docs/regulatory/` |
| Runbooks | **DRAFT, untested; no drill ever run** | `docs/runbooks/` |

## Architecture: Three-Zone Sovereign Runtime (target design)

Notes in parentheses give the state in this tree; see the spec that plans each component.

```
Zone A — Thinking (GPU, LLMs, LangGraph, HMM, VectorDB)
  zone-a/cognitive-core/      Python: LangGraph Blue/Red/Judge/Reflector debate (implemented)
  zone-a/regime-detector/     Python: HMM 5-state market regime classifier (implemented; hmm.py shim + regime_detector/ package)
  zone-a/vector-db/           ChromaDB: adversarial scenario embeddings (planned; empty package)

Zone B — Execution (Aegis, SOR, HSM, FIX)
  zone-b/sensory-array/       Rust: WebSocket L2 ingestor, QuestDB writer (implemented, hot path)
  zone-b/aegis/               Rust: deterministic PTCs, HSM signing, kill switches (spec: docs/specs/phase_3_aegis_execution.md; placeholder in this tree, implementation on pkg-e/aegis)
  zone-b/execution-motor/     Python: order execution library; Smart Order Router deferred (Phase 3 spec §10; no service entrypoint yet)

Zone C — Compliance (audit logs, HITL terminal, DORA reporting)
  zone-c/audit-logger/        Python library: append-only, hash-chained audit logs, KL monitor functions (implemented; Phase 4 spec §8)
  zone-c/compliance-manifest/ Python library: per-trade manifest pipeline (implemented; Phase 4 spec §9)
  zone-c/sharp-gate/          Python library: SHARP promotion state machine (implemented)
  zone-c/hitl-interface/      Next.js: operator HITL terminal (implemented; backend contract PROPOSED)

Shared
  shared/proto/               Protobuf message definitions; Aegis service definition proposed in Phase 3 spec Appendix A
  backtesting/                Python: event-driven sim, Monte Carlo, regime-aware WFA (implemented, synthetic data only)
  infrastructure/             Docker Compose, .env.example
  docs/                       ADRs, specs, regulatory drafts, runbooks, processes
```

## Quick Start

Per-package commands (Python 3.12; run each from its directory, use your own virtualenv):

```bash
# Lint: repo ruff.toml, run from each package directory (isort first-party detection depends on the cwd)
cd agent-core/zone-a/regime-detector && ruff check --config ../../../ruff.toml .

# Python packages (each has its own requirements-dev.txt / pyproject.toml)
cd agent-core/zone-a/cognitive-core   && pip install -r requirements.txt -r requirements-dev.txt && python -m pytest
cd agent-core/zone-a/regime-detector  && pip install -r requirements-dev.txt && python -m pytest
cd agent-core/zone-b/execution-motor  && pip install -r requirements-dev.txt && python -m pytest && python -m mypy .
cd agent-core/zone-c/audit-logger     && pip install -r requirements.txt pytest && python -m pytest   # integration tests need Docker

# Backtesting (from agent-core/): tests, lint, types, smoke CLI
cd agent-core && pip install -r backtesting/requirements-dev.txt
python -m pytest backtesting -c backtesting/pytest.ini --cov=backtesting --cov-config=backtesting/.coveragerc
python -m mypy --config-file backtesting/mypy.ini backtesting
python -m backtesting.engine --help        # smoke CLI; python -m backtesting.calibrate_ptc needs a real dataset

# Sensory array (Rust; no protoc needed)
cd agent-core/zone-b/sensory-array && cargo fmt --check && cargo clippy --locked --all-targets -- -D warnings && cargo test --locked

# HITL operator terminal (Node >= 20.9; Dockerfile uses Node 22)
cd agent-core/zone-c/hitl-interface && npm ci && npm run typecheck && npm run lint && npm test && npm run build
HITL_DEMO_MODE=true HITL_JWT_SECRET="$(openssl rand -base64 48)" npm run dev   # local demo, synthetic data

# Protobuf: lint and breaking-change check against a git ref (needs buf, or Docker)
cd agent-core/shared/proto && buf lint && BASE_REF=origin/main bash check-breaking.sh
bash generate.sh                          # Python stubs (needs: pip install grpcio-tools)
```

### Docker Compose

```bash
cd agent-core/infrastructure
cp .env.example .env                      # fill every empty value; the file has no real secrets
docker compose config                     # fails loudly on a missing ${VAR:?} secret
docker compose up -d                      # base file: no host ports except HITL on 127.0.0.1:3000
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d   # local dev: host ports, plaintext Aegis
```

Verified: `docker compose config` (base and dev override), and starting `vector-db`, `redis`, `questdb`, `postgres-audit`
alone (all reached healthy; the audit init script created its roles). **Not verified:** starting the whole stack. Known
blockers for a working end-to-end stack: Aegis needs owner-supplied `limits.json`/`identities.json`, TLS material and an HSM
(or the dev signer); cognitive-core only speaks plaintext gRPC, so it can reach Aegis only with the dev override
(`AEGIS_INSECURE_DEV=1`); nothing pushes reference data to Aegis yet; the HITL backend does not exist; cognitive-core needs
an LLM route (ADR-003, compose adds a proposed `zone-a-llm-egress` network); `execution-motor` has no entrypoint.
`.gitattributes` forces LF on `*.sh`/`*.sql`/Dockerfiles: a CRLF checkout breaks the Postgres init script inside the container.

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

- **Aegis is the only component that may authorise an order** (target). Zone A must hold no trading/signing/broker credentials. Compose no longer injects LLM provider keys: cognitive-core uses AWS Bedrock with IAM-role auth (ADR-002). The route (VPC endpoint vs gateway vs direct API) is still undecided in `docs/adr/ADR-003-zone-a-llm-access.md` (Proposed), and compose carries a proposed `zone-a-llm-egress` network until it is.
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

| Phase | Status (2026-09-21) | Spec | Notes |
|---|---|---|---|
| Step 0: unified-memory | Done (tooling) | n/a | Claude Code memory plugin; unrelated to trading logic |
| Phase 0: Foundation scaffold | **Done** (merged): directory scaffold, compose, protos, docs, CI | n/a | Compose/proto/CI gaps remain (see status table) |
| Phase 1: Sensory Array + Regime Detector | **Code implemented; regime-detector tests pass (134); sensory-array never run against Polygon** | `docs/specs/phase_1_sensory_array.md` | Spec updated to the real module maps |
| Phase 2: Cognitive Core | **Code implemented; unit tests with mocked LLMs**; not integrated with a live Aegis | `docs/specs/phase_2_cognitive_core.md` | LLM access unresolved (ADR-003) |
| Phase 3: Aegis + Execution | **Aegis on branch `pkg-e/aegis` (not in this tree); execution-motor is a library** | `docs/specs/phase_3_aegis_execution.md` | Reference-data feed and broker gateway not wired |
| Phase 4: Backtesting + Compliance | **Libraries implemented** (backtesting, audit-logger, compliance-manifest, sharp-gate, HITL terminal); no calibrated result exists | `docs/specs/phase_4_backtesting_compliance.md` | Needs real data before any threshold can be called calibrated |
| Phase 5: Canary + Production | **Not started.** Spec drafted; go-live is an owner decision | `docs/specs/phase_5_canary_production.md` | |

## Documentation index

- Specs: `docs/specs/` (phases 1-5)
- Decisions: `docs/adr/` (ADR-001, ADR-002 Accepted with status notes on open conflicts; ADR-003, ADR-004 **Proposed**)
- Process: `docs/processes/sharp-promotion.md`
- Regulatory drafts: `docs/regulatory/`
- Runbooks (DRAFT, untested): `docs/runbooks/`
- The original design "blueprint" (`agentic_fintech_blueprint_v2.md`) is **not in this repository**; it was an external document read in the initial session (see root `MEMORY.md`). References to a "blueprint Section 3" cannot be resolved from the repo.
