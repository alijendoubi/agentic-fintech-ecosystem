import { describe, expect, it } from "vitest";
import { deriveCsrfToken, isAllowedOrigin, verifyCsrfToken } from "@/lib/security/csrf";
import { buildCsp, generateNonce, staticSecurityHeaders } from "@/lib/security/headers";
import { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";

const SECRET = "k9Xv2mQ7pL4tR8wZ1nB6cY3dF5hJ0aGs";

describe("CSRF token", () => {
  it("accepts the token derived for the same session", () => {
    const token = deriveCsrfToken(SECRET, "session-jwt");
    expect(verifyCsrfToken(SECRET, "session-jwt", token)).toBe(true);
  });
  it("rejects a token for another session, another secret, or garbage", () => {
    const token = deriveCsrfToken(SECRET, "session-jwt");
    expect(verifyCsrfToken(SECRET, "other-session", token)).toBe(false);
    expect(verifyCsrfToken(`${SECRET}x`, "session-jwt", token)).toBe(false);
    expect(verifyCsrfToken(SECRET, "session-jwt", "nope")).toBe(false);
  });
  it("rejects a missing token", () => {
    expect(verifyCsrfToken(SECRET, "session-jwt", null)).toBe(false);
    expect(verifyCsrfToken(SECRET, "session-jwt", "")).toBe(false);
  });
});

describe("isAllowedOrigin", () => {
  const base = { host: "hitl.example.test", allowedOrigins: [] as string[] };
  it("allows a same-host origin", () => {
    expect(isAllowedOrigin({ ...base, origin: "https://hitl.example.test" })).toBe(true);
  });
  it("denies missing, null and unparsable origins", () => {
    expect(isAllowedOrigin({ ...base, origin: null })).toBe(false);
    expect(isAllowedOrigin({ ...base, origin: "null" })).toBe(false);
    expect(isAllowedOrigin({ ...base, origin: "::::" })).toBe(false);
  });
  it("denies a foreign origin and a missing host", () => {
    expect(isAllowedOrigin({ ...base, origin: "https://evil.test" })).toBe(false);
    expect(isAllowedOrigin({ origin: "https://hitl.example.test", host: null, allowedOrigins: [] })).toBe(false);
  });
  it("allows an explicitly allow-listed origin", () => {
    expect(isAllowedOrigin({ ...base, origin: "https://ui.corp.test", allowedOrigins: ["https://ui.corp.test"] })).toBe(true);
  });
});

describe("SlidingWindowRateLimiter", () => {
  it("allows up to the limit then blocks with a retry hint", () => {
    let now = 0;
    const limiter = new SlidingWindowRateLimiter(2, 60_000, () => now);
    expect(limiter.check("a").allowed).toBe(true);
    expect(limiter.check("a").allowed).toBe(true);
    const blocked = limiter.check("a");
    expect(blocked.allowed).toBe(false);
    expect(blocked.retryAfterSec).toBe(60);
    now = 61_000;
    expect(limiter.check("a").allowed).toBe(true);
  });
  it("tracks keys independently", () => {
    const limiter = new SlidingWindowRateLimiter(1, 1000, () => 0);
    expect(limiter.check("a").allowed).toBe(true);
    expect(limiter.check("b").allowed).toBe(true);
    expect(limiter.check("a").allowed).toBe(false);
  });
});

describe("security headers", () => {
  it("builds a strict CSP with frame-ancestors none and a nonce", () => {
    const csp = buildCsp({ nonce: "abc", isDevelopment: false });
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("'nonce-abc'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).not.toContain("unsafe-eval");
    expect(csp).not.toMatch(/script-src[^;]*unsafe-inline/);
  });
  it("only allows unsafe-eval in development", () => {
    expect(buildCsp({ nonce: "abc", isDevelopment: true })).toContain("'unsafe-eval'");
  });
  it("sets no-referrer, nosniff, deny framing and HSTS in production", () => {
    const headers = staticSecurityHeaders(true);
    expect(headers["Referrer-Policy"]).toBe("no-referrer");
    expect(headers["X-Content-Type-Options"]).toBe("nosniff");
    expect(headers["X-Frame-Options"]).toBe("DENY");
    expect(headers["Strict-Transport-Security"]).toBeDefined();
    expect(staticSecurityHeaders(false)["Strict-Transport-Security"]).toBeUndefined();
  });
  it("generates unique nonces", () => {
    expect(generateNonce()).not.toBe(generateNonce());
  });
});
