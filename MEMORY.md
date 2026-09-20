---
memory_version: 1
project_id: "agentic-fintech-ecosystem-x7k2m9"
project_name: "Agentic Fintech Ecosystem"
branch: "chore/base-fixes"
updated_at: "2026-09-19"
last_session: 3
health: 1
git_head: "see git log"
resume_priority: continuation_first
---

# Project Memory

## Snapshot
| Field | Value |
|---|---|
| Project | Agentic Fintech Ecosystem |
| Stack | Rust + Python + TypeScript \| LangGraph + ChromaDB + QuestDB + Alpaca |
| Goal | Greenfield autonomous US equities trading system, aiming at MiFID II RTS 6 / DORA / EU AI Act alignment (no compliance claim is verified; requires qualified legal review) |
| Status (2026-09-19) | Phases 1-2 code exists (sensory-array, regime-detector, cognitive-core). Aegis is a 3-line placeholder; execution-motor, audit-logger, compliance-manifest, backtesting are empty packages; hitl-interface does not exist. Specs for Phases 3-5 drafted (docs only). System cannot trade |
| Next immediate step | Owner decisions listed in the docs pass (ADR-003, ADR-004, Phase 3 spec §13); then implement Aegis per `agent-core/docs/specs/phase_3_aegis_execution.md` |

## Where We Left Off
- 2026-09-19 docs pass (PKG-S): wrote Phase 3/4/5 specs, ADR-003/ADR-004 (Proposed), 4 DRAFT runbooks, flagged regulatory docs (status boxes, claims-vs-implementation tables, TODO(owner) markers), ptc-calibration UNSUBSTANTIATED banner, README/CLAUDE.md drift fixes.
- Code fixes are being done by other work packages on their own branches; this file is docs-only.
- Open owner decisions: see "Blockers".

## Blockers
| Status | Blocker | Owner | Since |
|---|---|---|---|
| Open | ADR-003: how Zone A reaches LLMs (README says zero keys; compose injects keys; network has no route) | Ali | 2026-09-19 |
| Open | ADR-004: what the HSM signs given Alpaca API key + secret auth | Ali | 2026-09-19 |
| Open | Production broker undecided (DORA doc says TBD; Alpaca is working choice) | Ali | 2026-09-19 |
| Open | Compliance Officer / Legal / Risk Manager roles unnamed | Ali | 2026-09-19 |
| Open | No historical dataset; PTC thresholds uncalibrated (ptc-calibration.md UNSUBSTANTIATED) | Ali | 2026-09-19 |
| Open | Legal advice on applicable regimes, EU AI Act dates/citations, DORA timelines not obtained | Ali | 2026-09-19 |

## Key Decisions
| Date | Decision | Why | Scope |
|---|---|---|---|
| 2026-05-14 | Greenfield replacement (not extending Trading-News) | Clean separation; no TypeScript legacy constraints on Rust/Python core | All |
| 2026-05-14 | US Equities as initial asset class | Polygon.io already integrated; SEC 15c3-5 relevance is a legal question (unverified) | Phase 1+ |
| 2026-05-14 | Mistral Large 2 for Blue/Red nodes, Claude Sonnet for Judge (ADR-002) | Two model families reduce correlated failures. Deployment route conflicts with repo, see ADR-003 (Proposed) | Phase 2 |
| 2026-05-14 | ChromaDB local for Vector DB | Zero-infrastructure to start; migrate to Qdrant at scale | Phase 2 |
| 2026-05-14 | Alpaca Markets as working execution broker | Commission-free, paper trading available. Production broker undecided | Phase 3 |
| 2026-05-14 | SoftHSM2 + PKCS#11 for dev HSM (ADR-001) | Standards-compliant. What the HSM signs is unresolved, see ADR-004 (Proposed) | Phase 3 |
| 2026-05-14 | unified-memory installed from source (not npm) | Package not published to npm registry yet; cloned from github:alijendoubi/unified-memory and npm linked | Step 0 |
| 2026-09-19 | PROPOSED (not decided): kill-switch latch model, fixed-point money, broker gateway, Option 2 LLM gateway | Drafts for owner in Phase 3 spec / ADR-003 / ADR-004 | Phase 3 |

## Key Files
"State" reflects the tree on 2026-09-19. Planned = does not exist yet.

| File | Purpose | State |
|---|---|---|
| `agent-core/infrastructure/docker-compose.yml` | 3-zone container orchestration | Exists; cannot start full stack (missing Dockerfiles, network/secret conflicts) |
| `agent-core/shared/proto/*.proto` (4 files) | Messages: snapshot, signal, order, manifest | Exists; no rpc; money as double |
| `agent-core/zone-b/sensory-array/src/*.rs` | Rust L2 ingestor, normalizer, QuestDB writer | Implemented; build not verified in the docs pass |
| `agent-core/zone-b/aegis/src/main.rs` | Aegis | 3-line placeholder. Planned: PTCs, kill switches, signing (Phase 3 spec) |
| `agent-core/zone-b/execution-motor/` | Execution | Empty package. Planned (Phase 3 spec §10) |
| `agent-core/zone-a/regime-detector/hmm.py` | HMM 5-state classifier | Implemented, no tests |
| `agent-core/zone-a/cognitive-core/graph.py` | LangGraph Blue/Red/Judge debate | Implemented; 11 tests pass (mocked LLMs) |
| `agent-core/zone-c/audit-logger/`, `compliance-manifest/` | Audit + manifest | Empty packages. Planned (Phase 4 spec) |
| `agent-core/zone-c/hitl-interface/` | HITL terminal | Does not exist. Planned (Phase 4 spec §10) |
| `agent-core/backtesting/` | Backtest engine, WFA, Monte Carlo | Empty package. Planned (Phase 4 spec) |
| `agent-core/docs/specs/phase_3..5_*.md` | Specs for Phases 3-5 | Drafted 2026-09-19 (DRAFT, owner decisions pending) |
| `agent-core/docs/adr/ADR-003, ADR-004` | Proposed decisions | Proposed |
| `agent-core/docs/runbooks/*.md` | Kill-switch drill, key rotation, DR, incident response | DRAFT, untested |
| `MEMORY.md` | This file | Active |
| `.claude/settings.json`, `.claude/hooks/*.js` | Claude hooks wired to unified-memory | Present on the `feat/unified-memory-install` branch (not on this base) |

## Active Work
| Item | Status | Last touched |
|---|---|---|
| Step 0: unified-memory install | Done (tooling; on separate branch) | 2026-05-14 |
| Phase 0: Foundation scaffold | Done (merged) | 2026-05-14 |
| Phase 1: Sensory Array + Regime Detector | Code implemented; build unverified; regime-detector untested | 2026-05-18 |
| Phase 2: Cognitive Core | Code implemented, 11 tests pass; not integrated with Aegis; LLM route unresolved | 2026-09-19 (merged PR #4) |
| Phase 3: Aegis + Execution | Not started (placeholder); spec drafted, owner decisions pending | 2026-09-19 |
| Phase 4: Backtesting + Compliance | Not started (empty packages); spec drafted | 2026-09-19 |
| Phase 5: Canary + Production | Not started; spec drafted; go-live is an owner decision | 2026-09-19 |
| Docs/regulatory hygiene (ALI-17/50/55/61 docs) | Done in the docs pass; awaiting owner review | 2026-09-19 |

## Recent Sessions
| Session | Date | Summary |
|---|---|---|
| 1 | 2026-05-14 | Read agentic_fintech_blueprint_v2.md (external document, not in repo). Explored existing Trading-News project (Express.js + Next.js + PostgreSQL + 12 API integrations). Decided on greenfield replacement targeting US equities. Installed unified-memory v1.0.0 from source (github:alijendoubi/unified-memory — not on npm). Created GitHub repo alijendoubi/agentic-fintech-ecosystem. Completed Phase 0: scaffold zone-a/b/c structure, docker-compose.yml (3-zone), 4 protobuf definitions, 5 regulatory docs, 2 ADRs, SHARP promotion process, CI/CD pipeline. |
| 2 | 2026-05-18 | Phase 1: Rust sensory-array + Python HMM regime-detector written (compile/test not verified at that time). Phase 2 cognitive-core followed on its own branch (merged as PR #4). |
| 3 | 2026-09-19 | Audit-driven docs pass (PKG-S): Phase 3/4/5 specs, ADR-003/004 (Proposed), draft runbooks, regulatory-doc hygiene (status boxes, claims vs implementation, TODO(owner) markers, ptc-calibration banner), README/CLAUDE.md/MEMORY.md drift fixes. No code changed. |
