# HITL backend (Zone C, ALI-156)

The REST service behind the operator terminal (`zone-c/hitl-interface`), implementing
`zone-c/hitl-interface/docs/api-contract.md`. It turns an operator's decision on an Aegis hold
into `Aegis.ResolveHold`, relays a released decision to execution-motor, and audits every attempt.
Nothing here is a verified regulatory claim.

```
terminal (Next.js, server side) --REST--> hitl-backend --mTLS gRPC--> Aegis  (ListHolds/GetHold/ResolveHold)
                                               |            \--mTLS--> execution-motor (ExecuteWithContext)
                                               +--> audit-logger (afe_audit, as afe_audit_app)
```

## Scope and what is refused (fail closed)

* **Single-approver holds only.** A hold needs two approvers when its quantity is at or above
  `HITL_FOUR_EYES_QUANTITY_THRESHOLD` shares (or, for a limit order, its notional is at or above
  `HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD`), or when Aegis itself requires a second approver. Aegis
  (ALI-164) then needs an `afe-hold-v1` approval signed with the second approver's own key, and no
  signing method exists yet. Such approvals are refused with `second_approver_signing_unavailable`;
  REJECT still works. Owner decision 2026-09-29: the second approval is a distinct authenticated OIDC subject recorded in the hash-chained audit log (not implemented yet; Aegis's signed `afe-hold-v1` requirement must be reconciled).
* **No distress classifier (out of scope).** The owner ruled a reverse-guardrail distress classifier
  out of scope on 2026-09-29. `ResolveHold.reverse_guardrail_distress_score` is always 0.0, and the
  audit record says `distress_scored: false, distress_classifier: out_of_scope`. The human-oversight
  controls are the four-eyes threshold and the cooling period.
* **Aegis identity.** Aegis binds `ResolveHold.operator_id` to the caller's certificate CN, so Aegis
  records `hitl-backend`, and the human (JWT `sub`) goes into the `note` and into every audit record.
* **Released holds lose the market snapshot.** The debate's snapshot is not retained for held signals,
  so the context sent to execution-motor carries a snapshot built from the held signal only (symbol,
  regime, time), labelled `snapshot-source: hold-signal-only` in the manifest's model versions.
  Owner decision 2026-09-29: retain it in Zone C for the audit retention period (not implemented yet).
* **State is in memory.** This matches Aegis's hold store. After a restart, final holds and
  idempotency keys are forgotten; Aegis has already dropped any resolved hold, so a replayed
  request cannot decide twice.
* **No TLS server of its own.** The service speaks plain HTTP on the internal `zone-c-hitl` network
  only; TLS is terminated by `hitl-proxy` (owner decision 2026-09-29, `infrastructure/hitl-proxy/hitl.conf`).
  The terminal calls `https://hitl-proxy:9443`, which forwards `/v1/*` and `/healthz` here. No host port.

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
* Audit: `hitl.decision.attempt` is written before Aegis is called; if that write fails, the
  request is refused with 503 `audit_unavailable`. `hitl.decision.result` records the Aegis
  outcome, the HITL override record and the relay result; `hitl.decision.denied` and
  `hitl.decision.replayed` cover the other paths.

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
