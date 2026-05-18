---
memory_version: 1
project_id: "agentic-fintech-ecosystem-x7k2m9"
project_name: "Agentic Fintech Ecosystem"
branch: "feat/phase-1-sensory-array"
updated_at: "2026-05-18"
last_session: 2
health: 1
git_head: "pending-commit"
resume_priority: continuation_first
---

# Project Memory

## Snapshot
| Field | Value |
|---|---|
| Project | Agentic Fintech Ecosystem |
| Stack | Rust + Python + TypeScript \| LangGraph + ChromaDB + QuestDB + Alpaca |
| Goal | Greenfield autonomous US equities trading system — MiFID II RTS 6 + SEC 15c3-5 compliant, 3-zone sovereign architecture |
| Status | Phase 1 IN PROGRESS — Rust sensory-array + Python HMM regime-detector implemented |
| Next immediate step | `cargo check` on sensory-array; run Python unit tests; open PR #3 |

## Where We Left Off
- File: `agent-core/zone-b/sensory-array/src/ingestor.rs` (complete)
- File: `agent-core/zone-a/regime-detector/hmm.py` (complete)
- Branch: `feat/phase-1-sensory-array` (off feat/phase-0-foundation)
- Status: All Phase 1 files written. Needs `cargo check` + Python test run + PR.
- Next: verify compile, run unit tests, open PR #3
- Open question: aiohttp not in original requirements — added as dependency for QuestDB REST queries

## Blockers
| Status | Blocker | Owner | Since |
|---|---|---|---|

## Key Decisions
| Date | Decision | Why | Scope |
|---|---|---|---|
| 2026-05-14 | Greenfield replacement (not extending Trading-News) | Clean separation; no TypeScript legacy constraints on Rust/Python core | All |
| 2026-05-14 | US Equities as initial asset class | Polygon.io already integrated; full compliance path via SEC 15c3-5 | Phase 1+ |
| 2026-05-14 | Mistral Large 2 for Blue/Red nodes, Claude claude-sonnet-4-6 for Judge | EU-native + different families reduce correlated failures | Phase 2 |
| 2026-05-14 | ChromaDB local for Vector DB | Zero-infrastructure to start; migrate to Qdrant at scale | Phase 2 |
| 2026-05-14 | Alpaca Markets as execution broker | Commission-free, FIX-capable, paper trading available | Phase 3 |
| 2026-05-14 | SoftHSM2 + PKCS#11 for dev HSM | Free, standards-compliant, identical API to CloudHSM | Phase 3 |
| 2026-05-14 | unified-memory installed from source (not npm) | Package not published to npm registry yet; cloned from github:alijendoubi/unified-memory and npm linked | Step 0 |

## Key Files
| File | Purpose | State |
|---|---|---|
| `agent-core/infrastructure/docker-compose.yml` | 3-zone container orchestration with network isolation | ✅ Created |
| `agent-core/shared/proto/market_snapshot.proto` | Level 2 snapshot + RegimeLabel enum | ✅ Created |
| `agent-core/shared/proto/trade_signal.proto` | Judge output: omega, E[V], cost model | ✅ Created |
| `agent-core/shared/proto/order_request.proto` | HSM-signed order + ExecAlgo enum | ✅ Created |
| `agent-core/shared/proto/compliance_manifest.proto` | Per-trade manifest with hash chaining | ✅ Created |
| `agent-core/zone-b/sensory-array/Cargo.toml` | Rust crate: tokio, tungstenite, tonic, statrs | ✅ Stub |
| `agent-core/zone-b/sensory-array/src/ingestor.rs` | Multi-threaded WebSocket L2 ingestor | ⏳ Phase 1 |
| `agent-core/zone-b/sensory-array/src/questdb_writer.rs` | ILP writer + AsOf Join pipeline | ⏳ Phase 1 |
| `agent-core/zone-b/aegis/Cargo.toml` | Rust crate: tonic, cryptoki, sha2, uuid | ✅ Stub |
| `agent-core/zone-b/aegis/src/ptc.rs` | Deterministic hard+soft PTCs | ⏳ Phase 3 |
| `agent-core/zone-a/regime-detector/hmm.py` | HMM 5-state regime classifier | ⏳ Phase 1 |
| `agent-core/zone-a/cognitive-core/graph.py` | LangGraph Blue/Red/Judge debate | ⏳ Phase 2 |
| `agent-core/zone-c/compliance-manifest/manifest.py` | Per-trade manifest pipeline | ⏳ Phase 4 |
| `agent-core/backtesting/engine.py` | Event-driven backtesting with WFA | ⏳ Phase 4 |
| `MEMORY.md` | This file — unified-memory canonical store | ✅ Active |
| `.claude/settings.json` | Claude hooks wired to unified-memory | ✅ Active |
| `.claude/hooks/*.js` | SessionStart/UserPromptSubmit/PostToolUse/Stop | ✅ Active |

## Active Work
| Item | Status | Last touched |
|---|---|---|
| Step 0: unified-memory install | ✅ Complete | 2026-05-14 |
| Phase 0: Foundation scaffold | ✅ Complete — PR #2 open | 2026-05-14 |
| Phase 1: Sensory Array | ⏳ Next | — |
| Phase 2: Cognitive Core | ⏳ Pending | — |
| Phase 3: Aegis + Execution | ⏳ Pending | — |
| Phase 4: Backtesting + Compliance | ⏳ Pending | — |
| Phase 5: Canary + Production | ⏳ Pending | — |

## Recent Sessions
| Session | Date | Summary |
|---|---|---|
| 1 | 2026-05-14 | Read agentic_fintech_blueprint_v2.md. Explored existing Trading-News project (Express.js + Next.js + PostgreSQL + 12 API integrations). Decided on greenfield replacement targeting US equities. Installed unified-memory v1.0.0 from source (github:alijendoubi/unified-memory — not on npm). Created GitHub repo alijendoubi/agentic-fintech-ecosystem. Completed Phase 0: scaffold zone-a/b/c structure, docker-compose.yml (3-zone), 4 protobuf definitions, 5 regulatory docs (MiFID II RTS 6, DORA, EU AI Act, PTC calibration, third-party register), 2 ADRs (HSM, LLM), SHARP promotion process, CI/CD pipeline. PR #1 (unified-memory) and PR #2 (Phase 0 foundation) both open on main. Phase 1 Sensory Array is next. |
