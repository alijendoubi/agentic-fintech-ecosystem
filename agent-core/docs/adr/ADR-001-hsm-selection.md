# ADR-001: HSM Selection for API Key Custody

**Date:** 2026-05-14
**Status:** Accepted
**Deciders:** Ali Jendoubi (Lead), Compliance Officer (TBD)

---

## Context

The Aegis Layer is the only zone authorized to sign exchange API requests. No other zone (including the Cognitive Core) holds API keys or signing material. The HSM selection determines the security baseline for all transaction authorization.

MiFID II RTS 6 and DORA ICT Risk Management require documented key management procedures.

## Decision

**Development/Staging:** SoftHSM2 v2.6+ with PKCS#11 interface.
- Library path: `/usr/lib/softhsm/libsofthsm2.so`
- Identical PKCS#11 API to production HSMs — code is portable
- Keys stored in `/var/lib/softhsm/tokens/` (volume-mounted, not baked into image)

**Production:** AWS CloudHSM (FIPS 140-2 Level 3).
- Deployed in the same VPC as Zone B (execution)
- 2-of-3 MPC threshold signing via `aws-cloudhsm-pkcs11` library
- Key rotation: 90-day cycle with zero-downtime hot-swap

**Rejected alternative:** Thales Luna Network HSM 7
- Viable but requires on-premise hardware; CloudHSM preferred for cloud-native Zone B

## Signing Scheme

```
2-of-3 MPC threshold ECDSA (secp256k1 / secp256r1)
  Shard 1: Aegis container (Zone B)
  Shard 2: HSM (CloudHSM / SoftHSM2)
  Shard 3: Cold backup (Zone C audit store — offline)
```

A valid signature requires any 2 of 3 shards. A compromise of the Aegis container alone cannot produce a valid signature.

## Key Rotation Procedure

1. Generate new key pair in HSM (new key ID)
2. Register new key with exchange broker (Alpaca API key rotation)
3. Canary: route 1% of orders to new key for 24h — verify signatures accepted
4. Promote new key to primary — retire old key ID
5. Archive old key ID in DORA ICT Risk Framework log

Rotation frequency: every 90 days, or immediately upon any suspected compromise.

## DORA Compliance Note

This procedure is part of the DORA ICT Risk Management Framework (see `dora-ict-risk-management-framework.md`). Key rotation events must be logged to Zone C audit store within 4 hours of completion.

---

## Status note (2026-09-19, appended; the Decision above is unchanged)

This ADR remains Accepted, but two parts of it are in **open conflict** with other repo facts and are unimplemented (Aegis is a placeholder):
1. The working broker (Alpaca, see `docker-compose.yml`) authenticates with an API key and secret, not with a signature an HSM can produce, so what the HSM signs is unresolved. Also, the broker key is issued by the broker and cannot be generated in the HSM as the rotation steps assume. See **ADR-004** (Proposed).
2. The "2-of-3 MPC threshold ECDSA" scheme has no design in the repo and is unverified against the chosen HSM; "Shard 3: Cold backup (Zone C audit store)" conflicts with README/compose describing Zone C as air-gapped from signing keys.

The "Compliance Officer (TBD)" decider is still unnamed: TODO(owner).
