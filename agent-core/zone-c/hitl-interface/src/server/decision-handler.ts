import type { HitlApiClient } from "@/lib/api/types";
import type { AppConfig } from "@/lib/config";
import { submitOperatorDecision } from "@/lib/decision-service";
import type { DecisionFailureCode } from "@/lib/decision-service";
import { CSRF_HEADER, isAllowedOrigin, verifyCsrfToken } from "@/lib/security/csrf";
import type { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";
import { decisionBodySchema, signalIdParamSchema } from "@/lib/signals/schema";
import { authenticate, jsonResponse } from "./http";

export interface DecisionHandlerDeps {
  readonly config: AppConfig;
  readonly client: HitlApiClient;
  readonly limiter: SlidingWindowRateLimiter;
  readonly now?: () => number;
}

const MAX_BODY_BYTES = 4096;

const FAILURE_STATUS: Readonly<Record<DecisionFailureCode, number>> = {
  forbidden: 403,
  unauthorized: 403,
  reason_required: 422,
  expired: 409,
  already_final: 409,
  duplicate_approver: 409,
  not_found: 404,
  backend_error: 502,
  timeout: 504,
  invalid_response: 502,
};

function deny(status: number, code: string, message: string, headers: Record<string, string> = {}): Response {
  return jsonResponse({ ok: false, code, message, denied: true }, status, headers);
}

async function readBody(request: Request): Promise<unknown> {
  if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) return undefined;
  const text = await request.text();
  if (text.length > MAX_BODY_BYTES) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/**
 * POST /api/holds/{holdId}/decision. Every gate fails closed and every failure
 * is a JSON denial; the only success path is the backend confirming the decision.
 */
export async function handleDecisionRequest(
  request: Request,
  holdId: string,
  deps: DecisionHandlerDeps,
): Promise<Response> {
  const { config } = deps;
  const originOk = isAllowedOrigin({
    origin: request.headers.get("origin"),
    host: request.headers.get("host"),
    allowedOrigins: config.allowedOrigins,
  });
  if (!originOk) return deny(403, "csrf", "Cross-origin request refused.");

  const auth = await authenticate(request, config);
  if (!auth.ok) return deny(401, "unauthenticated", "Sign in again.");
  const { session } = auth;

  if (!verifyCsrfToken(config.jwtSecret, session.token, request.headers.get(CSRF_HEADER))) {
    return deny(403, "csrf", "Missing or invalid CSRF token. Reload the page.");
  }

  const limit = deps.limiter.check(`decision:${session.sub}`);
  if (!limit.allowed) {
    return deny(429, "rate_limited", "Too many actions. Wait and retry.", { "retry-after": String(limit.retryAfterSec) });
  }

  const id = signalIdParamSchema.safeParse(holdId);
  if (!id.success) return deny(404, "not_found", "Signal not found.");

  const body = decisionBodySchema.safeParse(await readBody(request));
  if (!body.success) {
    const reasonIssue = body.error.issues.some((issue) => issue.path[0] === "reason");
    return reasonIssue
      ? deny(422, "reason_required", "A reason of at least 10 characters is mandatory.")
      : deny(400, "invalid_request", "Invalid request body.");
  }

  const outcome = await submitOperatorDecision(
    { client: deps.client, fourEyes: config.fourEyes, now: deps.now },
    session,
    id.data,
    body.data,
  );
  if (!outcome.ok) return deny(FAILURE_STATUS[outcome.code], outcome.code, outcome.message);
  return jsonResponse({ ok: true, signal: outcome.signal }, 200);
}
