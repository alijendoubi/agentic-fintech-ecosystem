# HITL backend REST contract (ASSUMED, PROPOSED)

Status: **PROPOSED**. No service implements this yet. It is the contract the operator terminal
(`zone-c/hitl-interface`) was built and tested against (`HttpHitlApiClient`, `MockHitlApiClient`).
The owner of the HITL backend must confirm or change it (`TODO(owner)`).

## 1. Where this sits

```
operator browser --> hitl-interface (Next.js, server side) --REST--> HITL backend --gRPC--> Aegis
                                                                          |
                                                                          +--> audit-logger (hash-chained)
```

* The terminal never talks to Aegis and never signs or trades. It only relays a human decision.
* A **hold** is an Aegis `AegisDecision` with `decision = DECISION_HELD_FOR_HUMAN`
  (`shared/proto/aegis.proto`, spec `docs/specs/phase_3_aegis_execution.md` section 4). The operator approves or rejects
  the **hold**; the resource id is `hold_id`.
* Approve maps to `Aegis.ResolveHold(hold_id, operator_id, second_approver_id, approve=true, note)`.
  Reject maps to `ResolveHold(..., approve=false, note)`. Aegis **re-runs all hard controls at release time**: a human can
  release a soft block but never override a hard block. If Aegis rejects at release the hold becomes `RELEASE_DENIED`.
* Fields the terminal deliberately does **not** send and the backend must own: `reverse_guardrail_distress_score`,
  `cooling_period_enforced`. Whether a second approver or a cooling period is mandatory is `TODO(owner)` in the Aegis spec;
  the terminal enforces four-eyes by a configurable size threshold (section 6) and the backend may require more.
* Approving a hold never bypasses Aegis: "approved" here means "released to Aegis for hard-control re-check and
  attestation", not "order sent".

## 2. Encoding

proto3 canonical JSON of `TradeSignal` / `AegisDecision`, lowerCamelCase:

* `int64` (timestamps, money, quantity, price) are **decimal strings**. A JSON number for these is rejected.
* Money, price and quantity are **int64 nanos** (units of 1e-9), e.g. `"quantityNanos": "100000000000"` = 100 shares.
  The terminal formats them with integer arithmetic only and compares four-eyes thresholds in nanos.
* `omega`, `pSuccess`, `pFailure`, `regimeConfidence` are doubles (ratios, not money).
* `expectedValue`, `rewardEstimate`, `riskEstimate` remain doubles because `trade_signal.proto` has no nanos counterpart
  for them; the terminal displays them only and never computes on them. `TODO(owner)`: add nanos fields.
* Enums are names (`BUY`, `TRENDING_BULL`, `REASON_UNUSUAL_ORDER_SIZE`, ...).
* All responses: `Content-Type: application/json`; unknown extra fields are ignored, missing/mistyped required fields make
  the terminal treat the response as invalid (deny).

## 3. Authentication

Every request carries `Authorization: Bearer <operator JWT>` (the same token the operator signed in with) so the backend can
**re-verify** it. The backend MUST:

* verify signature (HS256 with the shared secret today; `TODO(owner)`: OIDC/JWKS), expiry, and (if configured) `iss`/`aud`;
* require claims `sub`, `role` in {`approver`,`viewer`}, and `amr` containing `mfa`;
* allow `POST .../decisions` only for `role = approver`; identify the actor from the JWT `sub`, never from the body.

Optionally `X-Service-Token: <HITL_API_TOKEN>` authenticates the terminal itself (service to service). Use mTLS or a private
network in addition; the terminal does not implement mTLS (`TODO(owner)`).

## 4. Endpoints

### `GET /v1/holds?status=pending` -> 200 `{ "holds": [Hold] }`

Holds awaiting a decision (including ones past `validUntilNs` that the backend has not yet expired).

### `GET /v1/holds/{holdId}` -> 200 `Hold` | 404

`holdId` matches `^[A-Za-z0-9_-]{1,64}$`.

### `POST /v1/holds/{holdId}/decisions` -> 200 `Hold` (the updated hold) | error

Request:

```json
{
  "decision": "APPROVE",
  "reason": "free text, 10..1000 chars, no control characters",
  "approver": { "sub": "…", "role": "approver", "amr": ["pwd", "mfa"] },
  "clientRequestId": "uuid"
}
```

* `approver` is advisory (for cross-checking against the JWT); the JWT is authoritative.
* `clientRequestId` is an idempotency key: replaying it returns the original result and records **no second decision**.
* The 200 body MUST contain the actor's decision in `approvals` (the terminal treats a response that does not confirm
  the decision as a denial).

### Errors

Non-2xx with body `{ "error": { "code": "<machine code>", "message": "<safe text, <=500 chars>" } }`.

| HTTP | Meaning | Terminal treats as |
|---|---|---|
| 401 | token invalid/expired | not approved; sign in again |
| 403 | role/MFA not allowed | not approved |
| 404 | unknown hold | not found |
| 409 / 410 / 422 | policy refusal: expired, already final, duplicate approver, reason missing, hard control failed at release | refused, not approved (message shown) |
| 429 / 503 | throttled / unavailable | not approved |
| other 5xx, timeout, network error, schema violation | unknown | **not approved** (deny by default) |

The terminal has a timeout (`HITL_API_TIMEOUT_MS`, default 5000 ms). After a timeout on `POST` the outcome is unknown; the UI
says "not approved, reload to verify" and never shows success without an explicit confirming 200.

## 5. `Hold` object

| Field | Type | Notes |
|---|---|---|
| `holdId`, `signalId` | string | ids; `holdId` is the resource id (`AegisDecision.hold_id`, `signal_id`) |
| `symbol`, `side` | string, `BUY`/`SELL`/`SELL_SHORT` | |
| `createdAtNs`, `validUntilNs`, `holdExpiresAtNs` | int64 string | `holdExpiresAtNs = min(validUntilNs, now + hold_window)` per Aegis spec |
| `quantityNanos`, `priceLimitNanos` | int64 nanos string | `priceLimitNanos = "0"` means market order |
| `estimatedSpreadCostNanos`, `estimatedMarketImpactNanos`, `estimatedVenueFeesNanos`, `estimatedTotalCostNanos` | int64 nanos string | |
| `omega`, `pSuccess`, `pFailure`, `regimeConfidence` | number | |
| `expectedValue`, `rewardEstimate`, `riskEstimate` | number | display only, see section 2 |
| `regime` | enum | `RegimeLabel` names |
| `strategyId` | string | |
| `debateSummary` | string | proto `debate_summary` (Judge synthesis) |
| `debate` | `{blue, red, judge}` strings, optional | NOT in the proto today; needs Cognitive Core to supply per-role text. The terminal shows it when present |
| `heldReasons` | string[] | `ReasonCode` names of failed soft controls |
| `controls` | array of `{controlId, isHard, passed, reason, threshold, observed, detail}` | `AegisDecision.results` |
| `hitlStatus` | `PENDING` / `APPROVED` / `REJECTED` / `EXPIRED` / `RELEASE_DENIED` | see below |
| `requiredApprovals` | 1 or 2 | backend's requirement; the terminal uses the stricter of this and its size threshold |
| `approvals` | array of `{approverSub, decision, reason, decidedAtNs}` | append-only, includes rejections |

`hitlStatus`: `PENDING` = held, awaiting operators (partial approvals allowed); `APPROVED` = enough distinct approvals and
Aegis released it; `REJECTED` = an operator rejected (final; maps to `REASON_HOLD_REJECTED_BY_OPERATOR`);
`EXPIRED` = hold window elapsed (`REASON_HOLD_EXPIRED`); `RELEASE_DENIED` = approved by operators but Aegis re-check failed.

## 6. Rules the backend MUST enforce (the terminal only pre-checks)

1. Actor authenticated with MFA and `role = approver`; viewers cannot decide.
2. Reason mandatory (>= 10 chars after trimming).
3. **Expiry = reject:** no decision after `min(validUntilNs, holdExpiresAtNs)` (exclusive); expired holds become `EXPIRED`.
4. Reject is final; nothing changes a `REJECTED` / `APPROVED` / `EXPIRED` / `RELEASE_DENIED` hold.
5. Four-eyes: when `quantityNanos >= HITL_FOUR_EYES_QUANTITY_THRESHOLD` (or notional over the optional USD threshold)
   `requiredApprovals = 2` distinct `sub`s. The same `sub` cannot approve twice. A prior approver may still reject.
   On the second approval the backend calls `ResolveHold` with `operator_id` = first approver and `second_approver_id` = second.
6. **Audit log every attempt, including denials**, before responding: actor `sub`, role, `amr`, hold id, decision, reason,
   outcome/denial code, timestamp, `clientRequestId`, and the Aegis result (`HITLOverrideRecord` in the manifest).
   Fail closed: if the audit write fails, the decision is refused (`REASON_AUDIT_UNAVAILABLE`).
7. Idempotent by `clientRequestId`; race-safe (two simultaneous approvals by different people must not both finalize a
   one-approval signal twice, and one person's two tabs must not count twice).

## 7. Environment consumed by the terminal (all server-side)

`HITL_API_BASE_URL`, `HITL_API_TOKEN` (optional), `HITL_API_TIMEOUT_MS`, `HITL_JWT_SECRET`, `HITL_JWT_ISSUER`,
`HITL_JWT_AUDIENCE`, `HITL_FOUR_EYES_QUANTITY_THRESHOLD`, `HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD`. See `.env.example`.
