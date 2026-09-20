# ADR-004: What the HSM Signs, Given Broker API-Key Authentication

**Date:** 2026-09-19
**Status:** Proposed (NOT accepted; drafted for the owner to decide)
**Deciders:** Ali Jendoubi (Lead) — decision pending
**Tracks:** ALI-44
**Related:** ADR-001 (Accepted; status note appended), `docs/specs/phase_3_aegis_execution.md` §7

> Broker facts beyond what the repo shows (compose/env use an API key and secret for Alpaca) are marked TODO(owner) and must be verified against the broker's current documentation and agreement. Legal/regulatory statements **require qualified legal review**.

---

## Context

ADR-001 (Accepted) says: the HSM signs exchange API requests / orders; "a valid signature requires any 2 of 3 shards"; production is AWS CloudHSM with 2-of-3 MPC threshold ECDSA via `aws-cloudhsm-pkcs11`; the rotation procedure says "Register new key with exchange broker (Alpaca API key rotation)" and "route 1% of orders to new key ... verify signatures accepted".

Reality visible in the repo:
- `docker-compose.yml`: `execution-motor` receives `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`; `.env.example` lists the same. The working broker authenticates with a **key ID + secret presented by the caller** (a bearer-style credential), not by an ECDSA signature over each request.
- `order_request.proto`: `hsm_signature` ("PKCS#11 ECDSA over canonical order bytes") and `hsm_key_id` ("Key ID for verification"). Nothing says who verifies it. The broker will not.
- Aegis has no implementation; `cryptoki` is in `Cargo.toml`.
- The broker key is *issued by the broker*; it cannot be generated inside an HSM the way ADR-001's rotation step 1 ("Generate new key pair in HSM ... then register with broker") assumes.
- The DORA doc says the production broker is "TBD"; the third-party register lists Alpaca.
- ADR-001 also places a signing shard in the Zone C audit store, while README/compose describe Zone C as air-gapped from signing keys. And "2-of-3 MPC threshold ECDSA" across an HSM and a container is not a capability the repo shows or that a plain PKCS#11 library is described as offering (TODO(owner): verify with AWS documentation whether CloudHSM supports any threshold scheme; if not, ADR-001's scheme is unimplementable as written).

Consequence: **what the HSM signs is unresolved**, and with it the claim that only Aegis can cause an order to be sent.

## What we actually need (security goals)

1. No order reaches the broker unless Aegis approved exactly that order (unbypassable by a compromised Zone A or execution-motor).
2. Approvals are non-replayable and short-lived.
3. Signing keys are non-extractable and rotatable, with an audit trail.
4. Compromise of one component should not yield standing ability to trade.

## Options

### A. Attestation only (HSM signs an Aegis approval; execution-motor holds the broker key)
- HSM signs a canonical attestation; execution-motor verifies it before calling the broker with its own broker credentials.
- Simple. But execution-motor (larger surface: SOR, algos, network I/O) holds the broker key. Anyone who compromises it can trade without Aegis. Goal 1 is not met against that adversary; it only protects against bugs and Zone A.

### B. Aegis holds the broker credential and places orders itself
- Meets goal 1 strongly, but merges routing/algo logic into the smallest, most critical component, hurting its <50 ms budget and auditability. The credential is still in process memory at use time (an HSM can store it wrapped, but the value must be presented to the broker in clear).

### C. Attestation + broker gateway (PROPOSED)
- HSM signs the attestation over canonical order bytes (Phase 3 spec §7 defines `afe-attest-v1`, 5 s expiry, single-use nonce).
- A small **broker gateway** is the only holder of broker credentials. For each request it: verifies the HSM signature with the public key, checks expiry and single use, checks that the outgoing order fields equal the attested fields exactly, then adds broker authentication and forwards. It also permits risk-reducing actions the state machine allows (cancels on kill-switch levels).
- execution-motor holds **no** broker credentials. Compromising it alone cannot place an unattested order. Compromising the gateway is the remaining high-value target; it is deliberately tiny and single-purpose. It may live inside the Aegis container (fewer components) or as a separate container (separate blast radius); recommend separate.
- What the HSM protects here: attestation keys (non-extractable). The broker secret itself is protected by ordinary secret management (and optionally stored wrapped), not by HSM signing. Documentation must say so honestly.
- Residual risk: the gateway process holds the broker secret in memory; broker-side controls (e.g. restricting the key to specific IPs or permissions, if the broker offers them: TODO(owner) verify) add defence in depth.

### D. Change the broker/authentication model
- If a broker with per-request signed authentication or certificate-based sessions is chosen, the HSM could sign the real broker requests. That is a broker-selection question (TODO(owner)); FIX-style sessions authenticate at logon, not per order. Not assumed here.

### E. Drop the HSM
- Use a software secret manager only. Rejected as a default: the audit and rotation story of ADR-001 would be lost, and attestation keys benefit from non-extractability. The owner may still choose it for the paper stage.

## PROPOSED decision

Adopt **Option C**, with:
1. Attestation signing key in the HSM (SoftHSM2 in dev, production HSM per Phase 5 §6), single-key ECDSA P-256 (mechanism support TODO(owner) verify).
2. ADR-001's "2-of-3 MPC threshold" and Zone-C shard **suspended until a concrete design is supplied**; the single HSM-held key plus dual-control on key administration stands in. (Owner may instead supply a threshold design; this ADR does not judge it feasible.)
3. Rotation (`docs/runbooks/key-rotation.md`) splits into two procedures: (a) **attestation key** rotation, fully under our control (new key id, gateway trusts both public keys during overlap); (b) **broker credential** rotation, controlled by the broker (generate new key at the broker, load into the gateway's secret store, verify, revoke the old one). ADR-001's "route 1% of orders to new key" applies to neither as written.
4. `order_request.proto` fields `hsm_signature`/`hsm_key_id` are kept and mirror the attestation; new `Attestation` message per Phase 3 Appendix A.

## Consequences

- Positive: goal 1 holds against a compromised execution-motor; honest description of what the HSM protects; cross-language verifiable canonical format.
- Negative: one more component to build, secure, monitor and include in the DORA ICT asset inventory; extra hop of latency for order submission (not in Aegis's 50 ms budget but in the end-to-end budget; unmeasured).
- Documents to update once decided: ADR-001 (supersede relevant sections), DORA doc asset inventory and key-rotation log semantics, README design principles, RTS6 template §3/§5, compose (new service, secret placement, network membership).
- Requires the owner to decide the broker first (Phase 5 checklist item 2).

## Open items for the owner

- [ ] Choose an option and set Status.
- [ ] TODO(owner): verify with the broker what credential scoping/restriction features exist and whether a per-order client id is supported.
- [ ] TODO(owner): verify HSM mechanism support and any threshold-signing capability.
- [ ] TODO(owner): decide gateway placement (inside Aegis or separate).
