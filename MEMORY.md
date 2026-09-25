---
memory_version: 1
project_id: "agentic-fintech-ecosystem-x7k2m9"
project_name: "Agentic Fintech Ecosystem"
branch: "main"
updated_at: "2026-09-25"
last_session: 4
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
| Status (2026-09-25) | Every package implemented and unit-tested, incl. Aegis (C01-C19, kill switches, signing, mTLS), the execution-motor gRPC service and refdata-bridge; paper-trade path wired (PR #8). Full dev compose stack first started 2026-09-25 (11/12 healthy; sensory-array needs a Polygon key). Never run against a real broker / LLM / feed; limits uncalibrated. Cannot trade for real |
| Next immediate step | Review/merge open PRs #9-#13 (+ ALI-15, ALI-17); owner decisions in Linear milestone "Go-Live Readiness Blockers" (ALI-152/153/154/155/171); fix GitHub billing (ALI-10) so CI runs |

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
| `agent-core/infrastructure/docker-compose.yml` | 3-zone container orchestration | Full dev stack started 2026-09-25 (ALI-167, fixes in PR #12) |
| `agent-core/shared/proto/*.proto` | Messages + `Aegis` and `ExecutionMotor` services | buf lint/breaking clean; money as `_nanos` int64 |
| `agent-core/zone-b/sensory-array/src/*.rs` | Rust L2 ingestor, normalizer, QuestDB writer | Implemented; image builds; never run against Polygon (ALI-165) |
| `agent-core/zone-b/aegis/` | Aegis risk gate | Implemented (C01-C19, kill switches, signing, mTLS, supervisor); tests pass in rust container |
| `agent-core/zone-b/execution-motor/` | Execution | gRPC service over mTLS; 360 tests pass; kill-switch reaction in PR #9 |
| `agent-core/zone-a/regime-detector/` | HMM 5-state classifier | Implemented and tested; HMAC-verified model store |
| `agent-core/zone-a/cognitive-core/` | LangGraph debate + runner service | Implemented; mocked-LLM tests; relays approvals to the motor |
| `agent-core/zone-c/audit-logger/`, `compliance-manifest/`, `sharp-gate/` | Audit, manifest, SHARP gate | Implemented libraries; not wired into the live path (ALI-161) |
| `agent-core/zone-c/hitl-interface/` | HITL terminal | Implemented (Next.js); its backend does not exist (ALI-156) |
| `agent-core/backtesting/` | Backtest engine, WFA, Monte Carlo | Implemented; synthetic data only (ALI-159) |
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
| Phase 1: Sensory Array + Regime Detector | Implemented and tested; per-symbol regime to Aegis in PR #11 | 2026-09-25 |
| Phase 2: Cognitive Core | Implemented, runner service, integrated with Aegis; LLM route unresolved (ALI-152) | 2026-09-22 (PR #8) |
| Phase 3: Aegis + Execution | Implemented; hardening PRs #9 (motor kill-switch), #10 (supervisor heartbeat) open | 2026-09-25 |
| Phase 4: Backtesting + Compliance | Libraries implemented; no real data; not wired into the live path | 2026-09-22 |
| Phase 5: Canary + Production | Not started; spec drafted; go-live is an owner decision | 2026-09-19 |
| Docs/regulatory hygiene (ALI-17/50/55/61 docs) | Done in the docs pass; awaiting owner review | 2026-09-19 |

## Recent Sessions
| Session | Date | Summary |
|---|---|---|
| 1 | 2026-05-14 | Read agentic_fintech_blueprint_v2.md (external document, not in repo). Explored existing Trading-News project (Express.js + Next.js + PostgreSQL + 12 API integrations). Decided on greenfield replacement targeting US equities. Installed unified-memory v1.0.0 from source (github:alijendoubi/unified-memory — not on npm). Created GitHub repo alijendoubi/agentic-fintech-ecosystem. Completed Phase 0: scaffold zone-a/b/c structure, docker-compose.yml (3-zone), 4 protobuf definitions, 5 regulatory docs, 2 ADRs, SHARP promotion process, CI/CD pipeline. |
| 2 | 2026-05-18 | Phase 1: Rust sensory-array + Python HMM regime-detector written (compile/test not verified at that time). Phase 2 cognitive-core followed on its own branch (merged as PR #4). |
| 3 | 2026-09-19 | Audit-driven docs pass (PKG-S): Phase 3/4/5 specs, ADR-003/004 (Proposed), draft runbooks, regulatory-doc hygiene (status boxes, claims vs implementation, TODO(owner) markers, ptc-calibration banner), README/CLAUDE.md/MEMORY.md drift fixes. No code changed. |
| 4 | 2026-09-25 | Linear triage (33 issues closed with commit evidence); PRs #9 motor kill-switch cancel (ALI-162), #10 supervisor heartbeat (ALI-163), #11 per-symbol regime (ALI-158), #12 first full compose bring-up (ALI-167), #13 live-stack tests (ALI-166); CI security scans + chromadb-client + cryptoki 0.12 (ALI-15/16); docs drift (ALI-17). Global git user.name fixed (ALI-12). |
