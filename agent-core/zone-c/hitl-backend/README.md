# HITL backend (Zone C, ALI-156)

The REST service behind the operator terminal (`zone-c/hitl-interface`), implementing
`zone-c/hitl-interface/docs/api-contract.md`. It turns an operator's decision on an Aegis hold
into `Aegis.ResolveHold`, relays a released decision to execution-motor, and audits every attempt.
Nothing here is a verified regulatory claim.

```
terminal (Next.js, server side) --REST--> hitl-backend --mTLS gRPC--> Aegis  (ListHolds/GetHold/ResolveHold)
                                               |            \--mTLS--> execution-motor (ExecuteWithContext)
                                               +--> audit-logger (afe_audit, as afe_audit_app):
                                                    writes every step, reads hold.context.retained
```

## Scope and what is refused (fail closed)

* **Two approvers (owner decision 2026-09-29, DECISIONS row 4).** A hold needs two approvals when
  its quantity is at or above `HITL_FOUR_EYES_QUANTITY_THRESHOLD` shares (or, for a limit order, its
  notional is at or above `HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD`). They come in separate requests
  from two **distinct** JWT `sub`s with role approver:
  * The first APPROVE is audited (`hitl.decision.first_approval`) and kept in memory; the reply is
    `200` with `hitlStatus: PENDING` and that approval in `approvals` (the terminal shows "1 of 2").
    Nothing reaches Aegis yet.
  * The same `sub` approving again gets `409 duplicate_approver`. A REJECT from anyone, including
    the first approver, is final at once.
  * The second distinct APPROVE calls `ResolveHold` once, with both approvals.
  * A pending first approval lapses with the hold (`min(validUntilNs, holdExpiresAtNs)`) or when
    Aegis no longer has the hold (`hitl.first_approval.lapsed`). After a restart it is forgotten
    (in memory, like the rest of the state) and must be given again; the audit log keeps it.
  * The cooling period applies to every approval.
  * Aegis identity: `operator_id` is bound to this service's certificate CN (`hitl-backend`), so
    the humans travel as `first_approval` and `second_approval` (`second_approver_id` = the second
    `sub`). Each is an `afe-hold-v1` Ed25519 signature, made when that approval was given, by the
    **attestor key** this service holds (`HITL_APPROVAL_ATTESTOR_*`), with `approver_id` = the
    JWT `sub` and `credential_ref = oidc-attested:<attestor_id>:<hex signature>`. Aegis lists the
    public key under `hold_attestors` in its identities file, accepts it only from this service's
    certificate, and itself refuses two approvals by the same subject (see the Aegis README).
  * **Trust model.** This trusts hitl-backend (which re-verifies every token and holds the key)
    and the token issuer. It is not a per-person hardware key: whoever controls this service's
    host, its attestor key or the IdP can approve as anyone. TODO(owner): whether that is enough
    for production. The tokens are still the terminal's HS256 tokens; OIDC/JWKS verification is
    not implemented (DECISIONS row 6, TODO(owner)).
  * Aegis checks both approvals are fresh (`approval_max_age_ms`, PROPOSED 300 s), so a hold
    window longer than that could make a slow second approval fail with `aegis_refused`.
  * Keep the hitl threshold at least as strict as Aegis's `hold_requires_second_approver`: when
    Aegis requires two and this service asks for one, Aegis refuses the release.
* **No distress classifier (out of scope).** The owner ruled a reverse-guardrail distress classifier
  out of scope on 2026-09-29. `ResolveHold.reverse_guardrail_distress_score` is always 0.0, and the
  audit record says `distress_scored: false, distress_classifier: out_of_scope`. The human-oversight
  controls are the four-eyes threshold and the cooling period.
* **Aegis identity.** Aegis binds `ResolveHold.operator_id` to the caller's certificate CN, so Aegis
  records `hitl-backend`, and the human (JWT `sub`) goes into the `note` and into every audit record.
* **Released holds carry the retained debate context (owner decision 2026-09-29, DECISIONS row 7).**
  When Aegis holds a signal, cognitive-core sends its full `ExecutionContext` (snapshot, model
  versions, Blue/Red/Judge texts) to `ExecutionMotor.RetainHeldContext` over the mTLS channel it
  already uses; execution-motor appends it to the audit log as `hold.context.retained`, keyed by
  `hold_id`/`signal_id`. No new network route: cognitive-core (Zone A) still cannot reach the audit
  database. On a release (checked before `ResolveHold`) this service reads the record with
  `afe_audit.RecordLookup` (hash and predecessor link re-checked) and binds it to Aegis's copy of
  the held signal (hold_id, signal_id, decision time, identical `TradeSignal`, sha256 of the bytes).
  * Found: execution-motor gets that context (`snapshot-source: retained-debate-context`,
    `retained-context-audit-seq`).
  * Absent (nothing was retained, e.g. the motor was unreachable): the old fallback, a snapshot
    built from the held signal only, labelled `snapshot-source: hold-signal-only` and
    `retained-context: absent`.
  * Tampered, two different records, or a record that does not match the hold: the release is
    refused (`409 retained_context_integrity|conflict|mismatch`); an unreadable store is `503
    retained_context_unavailable`. A human can still reject.
  * Retention period: nothing is deleted (the audit table is append-only) and no period is
    enforced. TODO(owner): legal confirmation of the audit retention period (issue #32).
* **State is in memory.** This matches Aegis's hold store. After a restart, final holds and
  idempotency keys are forgotten; Aegis has already dropped any resolved hold, so a replayed
  request cannot decide twice.
* **No TLS server.** The service runs on a private network. The terminal refuses plain http to a
  non-loopback host in production, so compose keeps this service behind the `hitl-backend`
  profile. Owner decision 2026-09-29: TLS terminated at a reverse proxy in front of the terminal and this service (not implemented yet).

## Rules enforced (contract section 6)

* JWT re-verified on every request: HS256, expiry, `iss`/`aud` (required in production), `sub`,
  `role` in {approver, viewer}, and `amr` containing `mfa`. Only approvers decide. There is also an
  optional `X-Service-Token`.
* The reason must be 10..1000 chars with no control characters. `clientRequestId` must be a UUID.
* Expiry at `min(validUntilNs, holdExpiresAtNs)`. Final holds stay final. An optional
  `HITL_COOLING_PERIOD_S` is counted from Aegis's decision time.
* Idempotency per operator: the same person with the same `clientRequestId` gets the original
  reply, and no second decision is made. An unknown outcome (5xx) is never cached.
* One lock serialises decisions, so two approvals cannot both resolve one hold.
* Audit: `hitl.decision.first_approval` (first of two) and `hitl.decision.attempt` (before Aegis
  is called) are written first; if that write fails, the request is refused with 503
  `audit_unavailable`. `hitl.decision.result` records the Aegis outcome, both approvers, the HITL
  override record and the relay result; `hitl.decision.denied`, `hitl.decision.replayed` and
  `hitl.first_approval.lapsed` cover the other paths. The manifest's HITL record names both
  approvers (`operator_id = "<first>+<second>"`).

## Environment

| Variable | Required | Meaning |
|---|---|---|
| `HITL_ENV` | no | `production` (default when unset) or anything else for dev |
| `HITL_BACKEND_LISTEN` | no | `host:port`, default `127.0.0.1:8090` |
| `HITL_JWT_SECRET` | yes | >= 32 chars, not a placeholder; same secret as the terminal |
| `HITL_JWT_ISSUER`, `HITL_JWT_AUDIENCE` | production | Checked on every token |
| `HITL_API_TOKEN` | no | If set (>= 16 chars), `X-Service-Token` must match |
| `AEGIS_TARGET`, `HITL_AEGIS_IDENTITY` | yes | Aegis `host:port`, and this service's certificate CN (role `hold-resolver`) |
| `HITL_AEGIS_TLS_CA/CERT/KEY` | production | mTLS to Aegis |
| `MOTOR_TARGET`, `HITL_MOTOR_TLS_CA/CERT/KEY` | production | Relay released decisions |
| `HITL_FOUR_EYES_QUANTITY_THRESHOLD`, `HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD` | quantity: default 0 | Four-eyes thresholds (shares, USD) |
| `HITL_COOLING_PERIOD_S` | no | Default 0 (none) |
| `HITL_APPROVAL_ATTESTOR_ID`, `HITL_APPROVAL_ATTESTOR_KEY_FILE` | production (set both or neither) | Attestor id in Aegis's `hold_attestors`, and a file with its 32-byte Ed25519 seed as 64 hex chars. The public key is logged at startup (`approval_attestor_loaded`). Without it (dev) approvals go to Aegis unattested |
| `POSTGRES_HOST/PORT/DB/USER/PASSWORD` | yes | Audit DB as `afe_audit_app` |
| `AFE_PROTO_DIR` | no | Generated stubs (set in the image) |

## Build and test

```bash
cd agent-core/zone-c/hitl-backend
pip install -r requirements-dev.txt
python -m pytest            # compiles the shared protos into a temp dir
ruff check --config ../../../ruff.toml . && python -m mypy hitl_backend tests
docker build -f agent-core/zone-c/hitl-backend/Dockerfile -t afe/hitl-backend:dev agent-core   # from repo root
```
