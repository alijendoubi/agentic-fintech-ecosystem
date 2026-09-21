import { isIP } from "node:net";
import { SESSION_COOKIE, extractToken } from "@/lib/auth/identity";
import { verifyOperatorToken } from "@/lib/auth/verify";
import type { AuthResult } from "@/lib/auth/verify";
import type { RevocationList } from "@/lib/auth/revocation";
import type { AppConfig } from "@/lib/config";

export function jsonResponse(body: unknown, status: number, extraHeaders: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...extraHeaders },
  });
}

/** Reads one cookie value from a Cookie header without decoding surprises. */
export function readCookie(cookieHeader: string | null | undefined, name: string): string | undefined {
  if (!cookieHeader) return undefined;
  for (const part of cookieHeader.split(";")) {
    const index = part.indexOf("=");
    if (index === -1) continue;
    if (part.slice(0, index).trim() === name) return part.slice(index + 1).trim();
  }
  return undefined;
}

export function sessionCookie(token: string, maxAgeSec: number, isProduction: boolean): string {
  const attrs = ["HttpOnly", "SameSite=Strict", "Path=/", `Max-Age=${Math.max(0, Math.floor(maxAgeSec))}`];
  if (isProduction) attrs.push("Secure");
  return `${SESSION_COOKIE}=${token}; ${attrs.join("; ")}`;
}

export function clearedSessionCookie(isProduction: boolean): string {
  return sessionCookie("", 0, isProduction);
}

/** Verifies the operator from Authorization (gateway-injected) or the session cookie. Deny on anything else. */
export function authenticate(
  request: Request,
  config: AppConfig,
  revocations?: RevocationList,
): Promise<AuthResult> {
  return verifyOperatorToken(requestToken(request), config, new Date(), revocations);
}

/** The raw operator token of a request (Authorization Bearer first, then the session cookie), if any. */
export function requestToken(request: Request): string | null {
  return extractToken({
    authorization: request.headers.get("authorization"),
    cookie: readCookie(request.headers.get("cookie"), SESSION_COOKIE),
  });
}

const UNKNOWN_CLIENT = "unknown";

/**
 * Rate-limit key for the caller. X-Forwarded-For is client-controlled on its left side, so it is only
 * read when `trustedProxyCount` proxies are known to append to it; the client is then the entry the
 * outermost trusted proxy appended, i.e. the Nth from the RIGHT. With 0 trusted proxies (the default)
 * the header is ignored and every caller shares one bucket ("unknown"), which cannot be spoofed around.
 */
export function clientKey(request: Request, trustedProxyCount: number): string {
  if (trustedProxyCount < 1) return UNKNOWN_CLIENT;
  const hops = (request.headers.get("x-forwarded-for") ?? "").split(",").map((hop) => hop.trim());
  const candidate = hops[hops.length - trustedProxyCount];
  return candidate !== undefined && isIP(candidate) !== 0 ? candidate : UNKNOWN_CLIENT;
}
