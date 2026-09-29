# Owner decisions, 2026-09-29

**Decided by:** Ali Jendoubi (Lead), in a working session.
**Scope:** This file records choices only. "Decided" does not mean "implemented". Each row says what exists today.
Regulatory statements here still **require qualified legal review**.

| # | Topic | Decision | Implemented? |
|---|---|---|---|
| 1 | Zone A LLM access (ADR-003) | **Option 2**: a key-holding LLM gateway outside Zone A. Zone A holds no provider keys. | Yes (PR #35): `llm-gateway` (LiteLLM) is the only holder of AWS credentials and the only service with LLM egress; cognitive-core reaches it on the internal `zone-a-llm-internal` network with a gateway key. Never run against real Bedrock; gateway TLS and the production IAM path are TODO(owner). |
| 2 | Broker credential custody (ADR-004) | **Option C**: a broker gateway in its **own container** is the only holder of broker keys. It verifies the attestation and exact order fields before forwarding. | Yes (PR #37): `zone-b/broker-gateway` is the only holder of `ALPACA_*`; it re-verifies the `afe-attest-v2` attestation and every order field before forwarding; execution-motor refuses to start with `ALPACA_*` set. Not run against the real Alpaca API. |
| 3 | Production broker | **Alpaca** (live after the paper stage). | Paper client only. |
| 4 | HITL second approver | A distinct authenticated **OIDC subject** per approval, recorded in the hash-chained audit log. The backend refuses the same subject twice. | No. Holds that need two approvers are refused (`second_approver_signing_unavailable`). Aegis currently expects a signed `afe-hold-v1` second approval (ALI-164), so the two must be reconciled. |
| 5 | Reverse-guardrail distress classifier | **Out of scope.** The score is always 0.0, and the audit record says `distress_classifier: out_of_scope`. The claim is withdrawn from the EU AI Act disclosure. | Yes (this change). |
| 6 | Terminal ↔ hitl-backend transport | **TLS at a reverse proxy** in front of both, with OIDC bearer tokens over HTTPS. | Yes in compose (`hitl-proxy`, nginx): the only HITL host port is HTTPS; the terminal calls hitl-backend via the proxy's internal TLS listener. Checked locally with dev certs and stand-ins; not yet run as containers. TODO(owner): production certificates and host name. |
| 7 | Held-signal snapshot retention | Keep the debate snapshot and context with the hold in Zone C for the **audit retention period**. The period itself needs legal confirmation. | No. Released holds carry a `hold-signal-only` snapshot. |
| 8 | Sign `strategy_id` into the attestation | **Yes**: bump the canonical format to `afe-attest-v2`. | Yes (PR #34): Aegis signs `afe-attest-v2` with `strategy_id`; execution-motor and broker-gateway verify it. |
| 9 | Default branch / stale branches (ALI-11) | `main` is the default; delete merged branches. | Branch deletion is left to the owner. |
| 10 | Container resource limits | mem/cpu/pids come from `${VAR:?}` env with no defaults. `.env.example` suggests PROPOSED values. | No. |
| 11 | Historical data (ALI-159) | **Polygon**. TODO(owner): check the licence permits backtesting use. | No. |
| 12 | Go-live stance | **Paper trading only** until calibration and legal review are done. | Stance; no code. |
| 13 | Open PRs #20–#25 | Merged 2026-09-29 without CI (GitHub Actions billing lock, ALI-10), after local verification. | Done. |

## Still open (need values or people, not a multiple-choice decision)

- GitHub Actions billing (ALI-10).
- Sizing parameters (`COGNITIVE_SIZER_*`), the Aegis `limits.json`, Aegis identities, the production HSM, and the motor caps.
- The HITL four-eyes thresholds and cooling period, the SHARP strategy map and approvers.
- Production credentials, the legal review, and review of the DRAFT seed scenarios (`zone-a/vector-db/seeds/`).
