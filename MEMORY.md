---
memory_version: 1
project_id: "agentic-fintech-ecosystem-x7k2m9"
project_name: "Agentic Fintech Ecosystem"
branch: "main"
updated_at: "2026-05-14"
last_session: 0
health: 1
git_head: "unknown"
resume_priority: continuation_first
---

# Project Memory

## Snapshot
| Field | Value |
|---|---|
| Project | Agentic Fintech Ecosystem |
| Stack | Rust + Python + TypeScript \| LangGraph + ChromaDB + QuestDB + Alpaca |
| Goal | Greenfield autonomous US equities trading system — MiFID II RTS 6 + SEC 15c3-5 compliant, 3-zone sovereign architecture |
| Status | Phase 0 — Foundation scaffolding in progress |
| Next immediate step | Complete Phase 0 scaffold, then Phase 1 Sensory Array |

## Where We Left Off
- File: `agent-core/` (being created)
- Function: directory scaffold
- Line: —
- Status: Phase 0 in progress
- Next: finish zone directory structure, docker-compose, protobuf defs, compliance docs
- Open question: none

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

## Key Files
| File | Purpose | State |
|---|---|---|
| `agent-core/infrastructure/docker-compose.yml` | 3-zone container orchestration | to be created |
| `agent-core/zone-a/cognitive-core/graph.py` | LangGraph Blue/Red/Judge debate graph | to be created |
| `agent-core/zone-a/regime-detector/hmm.py` | HMM 5-state regime classifier | to be created |
| `agent-core/zone-b/sensory-array/src/ingestor.rs` | Rust WebSocket L2 ingestor | to be created |
| `agent-core/zone-b/aegis/src/ptc.rs` | Deterministic pre-trade controls | to be created |
| `agent-core/zone-c/compliance-manifest/manifest.py` | Per-trade compliance manifest with hash chaining | to be created |
| `agent-core/backtesting/engine.py` | Event-driven backtesting with WFA | to be created |

## Active Work
| Item | Status | Last touched |
|---|---|---|
| Step 0: unified-memory install | completed | 2026-05-14 |
| Phase 0: Scaffold greenfield structure | in_progress | 2026-05-14 |
| Phase 1: Sensory Array | pending | — |
| Phase 2: Cognitive Core | pending | — |
| Phase 3: Aegis + Execution | pending | — |
| Phase 4: Backtesting + Compliance | pending | — |
| Phase 5: Canary + Production | pending | — |

## Recent Sessions
| Session | Date | Summary |
|---|---|---|
| 1 | 2026-05-14 | Familiarized with existing Trading-News project. Decided on greenfield replacement targeting US equities. Chose Mistral Large 2 (Blue/Red) + Claude claude-sonnet-4-6 (Judge). Installed unified-memory. Starting Phase 0 scaffold. |
