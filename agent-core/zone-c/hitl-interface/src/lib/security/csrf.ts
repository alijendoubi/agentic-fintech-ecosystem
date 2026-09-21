import { createHmac, timingSafeEqual } from "node:crypto";

/**
 * CSRF defence for mutation routes, in three layers:
 *  1. session cookie is SameSite=Strict + HttpOnly;
 *  2. Origin header must be present and match this host (or an allow-listed origin);
 *  3. a per-session synchronizer token (HMAC of the session token under the
 *     server secret) must be echoed in the x-csrf-token header.
 * All layers must pass; any doubt means deny.
 */

export const CSRF_HEADER = "x-csrf-token";
const DOMAIN = "afe-hitl-csrf-v1";

export function deriveCsrfToken(secret: string, sessionToken: string): string {
  return createHmac("sha256", secret).update(`${DOMAIN}\0${sessionToken}`).digest("hex");
}

export function verifyCsrfToken(secret: string, sessionToken: string, presented: string | null | undefined): boolean {
  if (!presented) return false;
  const expected = Buffer.from(deriveCsrfToken(secret, sessionToken), "utf8");
  const actual = Buffer.from(presented, "utf8");
  return expected.length === actual.length && timingSafeEqual(expected, actual);
}

export interface OriginCheckInput {
  readonly origin: string | null | undefined;
  readonly host: string | null | undefined;
  readonly allowedOrigins: readonly string[];
}

/** Fails closed: a missing Origin, an unparsable Origin, or a mismatch is a denial. */
export function isAllowedOrigin(input: OriginCheckInput): boolean {
  const { origin, host, allowedOrigins } = input;
  if (!origin || origin === "null") return false;
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return false;
  }
  if (allowedOrigins.includes(parsed.origin)) return true;
  return host !== null && host !== undefined && parsed.host === host;
}
