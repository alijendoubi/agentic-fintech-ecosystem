<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:1e3a5f,100:0f172a&height=200&section=header&text=Agentic%20Fintech%20Ecosystem&fontSize=42&fontColor=ffffff&fontAlignY=38&desc=Autonomous%20US%20Equities%20Trading%20—%20Design%20%2B%20Partial%20Implementation&descSize=16&descAlignY=58&animation=fadeIn" alt="Agentic Fintech Ecosystem" />

<img src="https://readme-typing-svg.demolab.com?font=Fira+Code&size=16&pause=1200&color=64B5F6&center=true&vCenter=true&width=700&lines=Zone+A%3A+cognitive+core+debates+a+trade+signal;Zone+B%3A+Aegis+decides+in+under+50ms%2C+fail-closed;Zone+C%3A+every+decision+is+hash-chained+and+human-reviewable" alt="typing animation" />

[![CI](https://github.com/alijendoubi/agentic-fintech-ecosystem/actions/workflows/ci.yml/badge.svg)](https://github.com/alijendoubi/agentic-fintech-ecosystem/actions/workflows/ci.yml)
![Status](https://img.shields.io/badge/status-not%20production%20ready-critical?style=flat-square)
![Rust](https://img.shields.io/badge/Rust-Zone%20B-orange?style=flat-square&logo=rust)
![Python](https://img.shields.io/badge/Python-3.12-blue?style=flat-square&logo=python)
![Next.js](https://img.shields.io/badge/Next.js-16-black?style=flat-square&logo=next.js)
![License](https://img.shields.io/badge/license-none%20yet-lightgrey?style=flat-square)

</div>


## What this is

A greenfield attempt at a **sovereign, three-zone autonomous trading system**: an LLM debate produces a trade
hypothesis, a deterministic risk gate decides whether it is allowed to exist, and every decision is logged in a way
that cannot be quietly edited afterward. It targets US equities, with MiFID II / DORA / EU AI Act considerations
carried as drafts, not compliance claims.

The design principle that matters most: **the language model never touches money.** It can only propose. A separate,
boring, deterministic Rust service — Aegis — is the only thing allowed to sign an order, and it fails closed on
anything it cannot verify.

## Architecture

```mermaid
flowchart LR
    subgraph ZoneA["Zone A — Thinking (no outbound internet except LLM egress)"]
        direction TB
        CC["cognitive-core<br/><i>LangGraph Blue / Red / Judge / Compression debate</i>"]
        RD["regime-detector<br/><i>5-state Gaussian HMM</i>"]
        VDB["vector-db<br/><i>ChromaDB precedent memory</i>"]
        RD --> CC
        VDB --> CC
    end

    subgraph ZoneB["Zone B — Execution (only zone allowed to move money)"]
        direction TB
        SA["sensory-array<br/><i>Rust L2 ingestor + normalizer</i>"]
        AEG["Aegis<br/><i>deterministic PTC gate, kill switches, HSM signing, mTLS</i>"]
        EM["execution-motor<br/><i>broker adapter, smart order routing</i>"]
        SA -.reference data.-> AEG
        AEG -->|signed attestation only| EM
    end

    subgraph ZoneC["Zone C — Compliance (append-only, air-gapped from signing keys)"]
        direction TB
        AL["audit-logger<br/><i>hash-chained Postgres, INSERT-only role</i>"]
        HITL["hitl-interface<br/><i>Next.js operator terminal</i>"]
        CM["compliance-manifest<br/><i>7-year retention pipeline</i>"]
    end

    CC -->|TradeSignal proto| AEG
    AEG -->|every decision| AL
    EM -->|fills| AL
    HITL -.approve / reject holds.-> AEG

    style ZoneA fill:#0f172a,stroke:#1e3a5f,color:#e2e8f0
    style ZoneB fill:#1a2e1a,stroke:#2d5a2d,color:#e2e8f0
    style ZoneC fill:#2e1a1a,stroke:#5a2d2d,color:#e2e8f0
```

Full package-by-package status, what's real versus placeholder, and exactly which commands were run and when:
**[`agent-core/README.md`](agent-core/README.md)**.

## Why it's built this way

| Decision | Reasoning |
|---|---|
| LLM proposes, never signs | A hallucination should produce a rejected proposal, not a filled order. |
| Aegis is Rust, not Python | The risk gate needs a deterministic, auditable, fast (<50ms target) decision path — no GC pauses, no dynamic dispatch surprises. |
| Money is fixed-point `int64` nanos | `f64` for money is a defect class this project refuses to ship. |
| Audit log is hash-chained, INSERT-only | A compromised app role must not be able to rewrite history, only get caught changing it. |
| Everything fails closed | Missing state, an unreachable dependency, or an unverifiable signature all resolve to "do nothing," never "assume it's fine." |

## Repository layout

```
agent-core/
├── zone-a/            cognitive-core · regime-detector · vector-db      (Python)
├── zone-b/            sensory-array · aegis · execution-motor           (Rust + Python)
├── zone-c/            audit-logger · compliance-manifest · sharp-gate · hitl-interface  (Python + TypeScript)
├── backtesting/       event-driven engine, walk-forward analysis, Monte Carlo
├── shared/proto/      protobuf contracts (buf-linted, breaking-change checked)
├── infrastructure/    docker-compose.yml, docker-compose.dev.yml, .env.example
└── docs/              specs, ADRs, regulatory drafts, runbooks — read the caveats on each
```

## Status

_Last updated 2026-09-25. Updated with every change; the detailed per-package table is in
[`agent-core/README.md`](agent-core/README.md#current-status-main-updated-2026-09-25)._

| | |
|---|---|
| **Works** | Every package is implemented and unit-tested. The paper-trade signal path (cognitive-core → Aegis → execution-motor) is wired. The full dev compose stack builds and runs (first started 2026-09-25). |
| **Verified live (dev stack)** | mTLS and identity roles, Redis → refdata-bridge → Aegis reference data, a real Aegis decision for a real signal, execution-motor refusing unapproved or tampered decisions, and a first kill-switch drill ([record](agent-core/docs/runbooks/drill-records/)). |
| **In review** | PRs #9–#18: kill-switch order cancellation and frozen-Aegis detection, supervisor healthcheck, per-symbol regime, compose bring-up fixes, live-stack tests, Redis ACLs, CI security scans, the drill tooling, and signed hold approvals. |
| **Not done** | Never run against a real broker, real LLMs or the real Polygon feed. Risk limits are uncalibrated. Owner decisions (LLM route, HSM vs broker gateway, credentials, legal review) are open. CI has never run on GitHub (billing lock). **Not production ready.** |

Open work is tracked in the Linear project "Agentic Fintech Ecosystem", milestone "Go-Live Readiness Blockers".

## Getting started

### Run the whole stack (dev)

```bash
cd agent-core/infrastructure/dev-tls && sh generate-dev-certs.sh --yes-i-know-this-is-dev-only
cd .. && cp .env.example .env      # fill every blank with DEV values; never commit it
# Aegis also needs a DEV limits.json in secrets/aegis-config/ (owner-supplied; see zone-b/aegis/README.md)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

# Live integration tests against the running stack
cd ../tests/live && pip install -r requirements.txt && AFE_LIVE_STACK=1 python -m pytest -v
```

The dev stack uses a mock broker and throwaway certificates. sensory-array needs a real `POLYGON_API_KEY`;
without one it fails closed on authentication, and everything else still runs.

### Build and test one package

Every package is independently buildable and testable:

```bash
# Rust (Aegis, sensory-array) — natively, or in the pinned rust:1.98-bookworm container (Aegis needs protoc)
cd agent-core/zone-b/aegis && cargo fmt --check && cargo clippy --all-targets -- -D warnings && cargo test

# Python packages — each has its own requirements-dev.txt
cd agent-core/zone-b/execution-motor && pip install -r requirements-dev.txt && python -m pytest

# HITL operator terminal (Node >= 20.9)
cd agent-core/zone-c/hitl-interface && npm ci && npm run typecheck && npm run lint && npm test

# Validate the compose topology without starting anything
cd agent-core/infrastructure && docker compose config
```

The full, per-package command reference — including which ones have actually been run and what their last verified
result was — lives in [`agent-core/README.md`](agent-core/README.md#quick-start).

## Documentation

| | |
|---|---|
| 📐 Specs | [`agent-core/docs/specs/`](agent-core/docs/specs) — phases 0 through 5 |
| 🧭 Architecture decisions | [`agent-core/docs/adr/`](agent-core/docs/adr) — two Accepted, two still Proposed |
| ⚖️ Regulatory drafts | [`agent-core/docs/regulatory/`](agent-core/docs/regulatory) — **not legal advice, not a compliance claim** |
| 🛠️ Runbooks | [`agent-core/docs/runbooks/`](agent-core/docs/runbooks) — drafts; the kill-switch runbook has had one dev-stack drill ([`drill-records/`](agent-core/docs/runbooks/drill-records)), DR and DORA runbooks never drilled |
| 🧪 Live-stack tests | [`agent-core/tests/live/`](agent-core/tests/live) — run against the running dev stack over real mTLS |
| 📋 Promotion process | [`agent-core/docs/processes/sharp-promotion.md`](agent-core/docs/processes/sharp-promotion.md) |

## License

No license file is currently committed to this repository. In its absence, default copyright applies: no one but the
owner has permission to copy, modify, or distribute this code. This is a gap to close before any public use, not an
implicit grant of rights.

<div align="center">
<img src="https://capsule-render.vercel.app/api?type=waving&color=0:1e3a5f,100:0f172a&height=100&section=footer" alt="" />
</div>
