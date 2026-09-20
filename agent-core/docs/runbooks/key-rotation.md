# Runbook: Key Rotation

> **DRAFT: not validated in any drill. UNTESTED.**
> No HSM integration, Aegis signing, broker gateway or key inventory exists yet. This runbook is a draft based on ADR-001 and the PROPOSED ADR-004 and must be revised once ADR-004 is decided. No rotation has ever been performed. Evidence tables are intentionally blank.

## Why this draft differs from ADR-001

ADR-001's rotation steps assume the HSM generates a key pair that is then registered with the broker and used to sign orders. The working broker (Alpaca) issues its own API key and secret; an HSM key cannot be that credential (see ADR-004). Under the PROPOSED ADR-004 (Option C) there are **two separate rotations**:

- **Procedure A: attestation signing key** (HSM-held, ours to generate).
- **Procedure B: broker credential** (issued by the broker, held by the broker gateway).

If the owner picks a different option in ADR-004, rewrite this runbook accordingly.

## Cadence and triggers

- Every 90 days [EXISTING, ADR-001], or immediately on suspected compromise. DORA doc §7 says emergency rotation within 4 h of suspected compromise [EXISTING, unvalidated].
- Also rotate when a person with access leaves, or after an HSM/gateway incident.
- The audit record of each rotation must reach Zone C within 4 hours of completion [EXISTING, ADR-001].

## Preconditions (all)

- [ ] Environment named (dev SoftHSM2 / paper / production) and confirmed; production rotations only after Phase 5 approval.
- [ ] Two authorised people present (dual control; names TODO(owner)).
- [ ] Trading state understood: rotate outside market hours, or with the system at a level that blocks new orders (PROPOSED: SOFT).
- [ ] Audit logger healthy; current key inventory (key ids, public keys, creation dates) exported.
- [ ] Rollback material identified (old key remains valid until step "retire").

## Procedure A: attestation signing key (PROPOSED)

1. **Generate** a new key pair inside the HSM (non-extractable, sign-only, ECDSA P-256; mechanism support TODO(owner)). Record new `key_id`, creation time, operator identities.
2. **Publish** the new public key to the broker gateway's trust list *alongside* the old one (overlap window). Do not remove the old one yet.
3. **Verify**: sign a canonical test attestation with the new key in the non-trading test path; the gateway verifies it; a tampered copy is rejected.
4. **Canary**: configure Aegis to sign with the new key. (ADR-001 proposes routing 1% of orders for 24 h; that scheme applies to a broker-accepted key and needs redefinition under ADR-004: TODO(owner). In paper, run a full session and confirm zero signature rejections.)
5. **Retire** the old key: remove it from the gateway trust list, disable it in the HSM, keep the public key archived for verifying historical attestations.
6. **Log**: append to the DORA doc §7 key rotation log (Key ID, created, rotated, reason, logged to Zone C) and to the Zone C audit store.
7. **Verify after**: `GetAegisState` shows `hsm_ok`; an approved paper order passes the gateway; old-key attestations are refused.

Rollback (before step 5): switch Aegis back to the old key id; remove the new public key from the trust list; log the aborted rotation.

## Procedure B: broker credential (PROPOSED)

1. In the broker's console, **create a new key/secret** for the correct environment (paper vs live; never reuse across environments). Record the broker-side key id (not the secret) in the inventory.
2. Load the secret into the gateway's secret store per the production secret-management design (Phase 5 §6; TODO(owner)). Never place it in a repository, chat, ticket or a compose `.env` committed anywhere.
3. **Verify** with a read-only call (account status), then a paper order with a valid attestation.
4. **Switch** the gateway to the new credential; observe a session.
5. **Revoke** the old credential at the broker; confirm it is rejected.
6. **Log** as in A.6. If the broker provides restrictions on keys (permissions, IP allow-lists), re-apply them to the new key (TODO(owner): verify what exists).

## Emergency rotation (suspected compromise)

1. Latch at least LOGIC/HARD (`kill-switch-drill.md` levels); cancel open orders; preserve evidence (logs, audit chain export).
2. Revoke the suspected credential at the source first (broker key revoked; HSM key disabled), then run A/B with a new key.
3. Treat as an incident: classify per DORA doc §4 and follow `incident-response-dora.md` (reporting duties **require qualified legal review**).

## Other secrets that need a rotation procedure (not yet documented)

Market-data key (Polygon), LLM provider keys (depends on ADR-003), HSM PIN, database passwords (`AUDIT_DB_PASSWORD`), `HITL_JWT_SECRET`, mTLS certificates. TODO(owner): owners and cadence.

## Evidence to record when a rotation or drill is executed (fill only from observation)

| Item | Value |
|---|---|
| Date, environment, executed by / witnessed by | |
| Procedure (A / B / emergency) and reason | |
| Old and new key ids (no secrets) | |
| Overlap window start/end | |
| Verification outputs (test attestation accepted; tampered rejected) | |
| Canary/paper session result | |
| Time of retirement and confirmation the old key fails | |
| Zone C audit record id(s) and time delivered (must be within 4 h) | |
| DORA doc §7 log entry made (yes/no, link) | |
| Deviations and runbook corrections | |
