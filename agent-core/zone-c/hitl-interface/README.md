# AFE HITL operator terminal

Next.js (App Router, TypeScript strict) console where a human approves or rejects AI-generated trade signals that Aegis
holds for a person (`DECISION_HELD_FOR_HUMAN`). The terminal decides nothing: it relays the operator's decision to a
backend and treats every error, timeout or unexpected answer as **not approved**.

Status: implemented and tested in isolation. Not verified against a real backend, Aegis or an IdP (none exist yet).
Backend contract: [`docs/api-contract.md`](docs/api-contract.md) (PROPOSED).

## Run

```bash
npm ci
npm run typecheck && npm run lint && npm test && npm run build

# local demo (synthetic data, DEMO banner; refused when NODE_ENV=production)
HITL_DEMO_MODE=true HITL_JWT_SECRET="$(openssl rand -base64 48)" npm run dev

# production image
docker build -t afe-hitl-interface .
docker run -p 3000:3000 -e HITL_JWT_SECRET=... -e HITL_API_BASE_URL=http://hitl-backend:4000 afe-hitl-interface
```

`.env.example` lists every variable. There are **no `NEXT_PUBLIC_*`** variables; the secret and backend URL never reach
the browser bundle (checked in the container: not present in `.next/static`).

## Security design

| Concern | Implementation |
|---|---|
| Refuse to start | `HITL_JWT_SECRET` unset, < 32 chars, placeholder, or < 8 distinct chars; missing/invalid `HITL_API_BASE_URL`; demo mode in production => process exits 1 (`src/lib/startup-guard.ts`). A throw in `instrumentation.ts` alone does not stop `next start`, hence the explicit exit |
| AuthN | HS256 JWT verified server-side (`jose`, algorithm pinned). Required: `sub`, `role` in approver/viewer, `exp`, `amr` containing `mfa`. `iss`/`aud` pinning (`HITL_JWT_ISSUER`/`HITL_JWT_AUDIENCE`, both required in production). Any failure => deny |
| AuthZ | Only `approver` + MFA can act; viewers are read-only. Checked in the page, the route, the decision service and the policy |
| Identity boundary | `TokenVerifier` / `extractToken` (`src/lib/auth`). **TODO(owner): SSO / IdP (OIDC + JWKS) integration is not implemented and no fake IdP is provided.** Today: paste a token at `/login`, or a fronting gateway injects `Authorization: Bearer` |
| CSRF | SameSite=Strict HttpOnly cookie + mandatory `Origin` match (or `HITL_ALLOWED_ORIGINS`) + per-session HMAC token in `x-csrf-token` |
| Rate limiting | In-memory sliding window per operator (mutations) and per client (login). **Per process only**: N replicas allow N x the limit and a restart resets it. Put a shared limiter in front for production |
| Headers | Nonce-based CSP (`strict-dynamic`, no script `unsafe-inline`), `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, nosniff, X-Frame-Options DENY, COOP/CORP, HSTS in production, `Cache-Control: no-store`. `style-src` keeps `'unsafe-inline'` (React inline styles) |
| Validation | zod on the request body (strict, unknown keys rejected), path ids, and every backend response (float money on the wire is rejected) |
| Fail closed | Backend error/timeout/invalid/unconfirmed response => "Not approved"; UI shows success only after an explicit `ok: true` |
| Transparency | Persistent "AI-generated signal" label on the queue and every detail page (EU AI Act Art. 50 transparency intent; no legal conclusion is made here) |

Decision rules (`src/lib/signals/policy.ts`, pure, unit-tested): mandatory reason (>= 10 chars), expiry checked against both
`validUntilNs` and `holdExpiresAtNs` (exclusive), reject is final, four-eyes by size threshold in int64 nanos, the same
person cannot approve twice (a prior approver may still reject). Expiry countdown uses server time so a skewed operator clock
cannot make an expired signal look live; the server re-checks on every action.

## Limits and open items

* `HITL_FOUR_EYES_QUANTITY_THRESHOLD` default (1000 shares) is a placeholder, not a calibrated value. TODO(owner).
* `debate.blue/red/judge` are not in `trade_signal.proto`; the terminal shows them only if the backend supplies them.
* Session cookie has no server-side revocation; it expires with the token. Logout only clears the cookie.
* No mTLS to the backend, no persistent storage, no metrics. TLS must be terminated in front (cookies are `Secure` in production).
* Accessibility was designed for (labels, roles, focus styles, contrast) and tested via Testing Library roles, but no
  screen-reader or automated axe audit was run.
