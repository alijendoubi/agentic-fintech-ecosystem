import { describe, expect, it } from "vitest";
import { ConfigError, loadConfig } from "@/lib/config";
import { RevocationList } from "@/lib/auth/revocation";
import { verifyOperatorToken } from "@/lib/auth/verify";
import { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";
import { handleLogin, handleLogout } from "@/server/auth-handlers";
import { authenticate } from "@/server/http";
import { TEST_SECRET, mintToken, testConfig } from "./helpers/tokens";

const ISSUER = "https://idp.example.test";
const AUDIENCE = "afe-hitl";
const HOST = "hitl.test";

const prod = testConfig({
  NODE_ENV: "production",
  HITL_API_BASE_URL: "https://backend.internal:4000",
  HITL_JWT_ISSUER: ISSUER,
  HITL_JWT_AUDIENCE: AUDIENCE,
});

let counter = 0;
const prodToken = (overrides: Parameters<typeof mintToken>[0] = {}) =>
  mintToken({ issuer: ISSUER, audience: AUDIENCE, jti: `jti-${++counter}`, ...overrides });

describe("production config requires issuer and audience", () => {
  const base = {
    HITL_JWT_SECRET: TEST_SECRET,
    HITL_API_BASE_URL: "https://backend.internal:4000",
    NODE_ENV: "production",
  };

  it("refuses to start without HITL_JWT_ISSUER or HITL_JWT_AUDIENCE", () => {
    expect(() => loadConfig({ ...base, HITL_JWT_AUDIENCE: AUDIENCE })).toThrow(/HITL_JWT_ISSUER/);
    expect(() => loadConfig({ ...base, HITL_JWT_ISSUER: ISSUER })).toThrow(/HITL_JWT_AUDIENCE/);
    expect(() => loadConfig(base)).toThrow(ConfigError);
  });

  it("keeps them optional outside production", () => {
    expect(loadConfig({ ...base, NODE_ENV: "development" }).jwtIssuer).toBeNull();
  });
});

describe("production token verification requires issuer and audience", () => {
  it("accepts a token carrying the pinned issuer and audience", async () => {
    expect((await verifyOperatorToken(await prodToken(), prod)).ok).toBe(true);
  });

  it("denies a token without issuer or audience", async () => {
    const noIss = await mintToken({ audience: AUDIENCE, jti: "n1" });
    const noAud = await mintToken({ issuer: ISSUER, jti: "n2" });
    expect((await verifyOperatorToken(noIss, prod)).ok).toBe(false);
    expect((await verifyOperatorToken(noAud, prod)).ok).toBe(false);
  });

  it("fails closed if a production verifier is somehow built without issuer/audience", async () => {
    const weak = { ...prod, jwtIssuer: null, jwtAudience: null };
    expect((await verifyOperatorToken(await prodToken(), weak)).ok).toBe(false);
  });
});

describe("token lifetime cap and jti", () => {
  it("defaults the lifetime cap to 15 minutes and lets it be tuned", () => {
    expect(prod.jwtMaxLifetimeSec).toBe(900);
    expect(testConfig({ HITL_JWT_MAX_LIFETIME_SEC: "300" }).jwtMaxLifetimeSec).toBe(300);
    expect(() => testConfig({ HITL_JWT_MAX_LIFETIME_SEC: "0" })).toThrow(ConfigError);
    expect(() => testConfig({ HITL_JWT_MAX_LIFETIME_SEC: "abc" })).toThrow(ConfigError);
  });

  it("accepts a token with jti and a lifetime at the cap", async () => {
    expect((await verifyOperatorToken(await prodToken({ expiresInSec: 900 }), prod)).ok).toBe(true);
  });

  it("requires a jti in production", async () => {
    const token = await mintToken({ issuer: ISSUER, audience: AUDIENCE });
    expect(await verifyOperatorToken(token, prod)).toEqual({ ok: false, code: "missing_jti" });
  });

  it("denies a lifetime (exp - iat) above the cap", async () => {
    const long = await prodToken({ expiresInSec: 901 });
    expect(await verifyOperatorToken(long, prod)).toEqual({ ok: false, code: "lifetime_too_long" });
    const day = await prodToken({ expiresInSec: 86_400 });
    expect(await verifyOperatorToken(day, prod)).toEqual({ ok: false, code: "lifetime_too_long" });
  });

  it("requires iat so the lifetime can be measured", async () => {
    const token = await prodToken({ omitIssuedAt: true });
    expect((await verifyOperatorToken(token, prod)).ok).toBe(false);
  });

  it("denies a future-dated iat that would stretch the real validity window", async () => {
    const token = await prodToken({ issuedAt: new Date(Date.now() + 3_600_000), expiresInSec: 900 });
    expect((await verifyOperatorToken(token, prod)).ok).toBe(false);
  });

  it("does not impose jti or the cap outside production", async () => {
    const token = await mintToken({ expiresInSec: 3600 });
    expect((await verifyOperatorToken(token, testConfig())).ok).toBe(true);
  });
});

describe("revocation list", () => {
  it("revokes by key until the token would have expired anyway", () => {
    let now = 1_000;
    const list = new RevocationList(() => now);
    list.revoke("j1", 1_100);
    expect(list.isRevoked("j1")).toBe(true);
    expect(list.isRevoked("j2")).toBe(false);
    now = 1_101;
    expect(list.isRevoked("j1")).toBe(false);
    expect(list.size).toBe(0);
  });

  it("stays bounded by evicting the entries closest to expiry", () => {
    const list = new RevocationList(() => 0, 3);
    for (let i = 1; i <= 5; i += 1) list.revoke(`j${i}`, 100 + i);
    expect(list.size).toBe(3);
    expect(list.isRevoked("j5")).toBe(true);
    expect(list.isRevoked("j1")).toBe(false);
  });

  it("denies a revoked token, keyed by jti or by token hash when there is no jti", async () => {
    const list = new RevocationList();
    const withJti = await prodToken();
    const first = await verifyOperatorToken(withJti, prod, new Date(), list);
    if (!first.ok) throw new Error("expected ok");
    expect(first.session.jti).toMatch(/^jti-/);
    list.revokeSession(first.session);
    expect(await verifyOperatorToken(withJti, prod, new Date(), list)).toEqual({ ok: false, code: "revoked" });

    const dev = testConfig();
    const noJti = await mintToken();
    const second = await verifyOperatorToken(noJti, dev, new Date(), list);
    if (!second.ok) throw new Error("expected ok");
    list.revokeSession(second.session);
    expect(await verifyOperatorToken(noJti, dev, new Date(), list)).toEqual({ ok: false, code: "revoked" });
  });
});

function form(path: string, fields: Record<string, string>, headers: Record<string, string> = {}): Request {
  return new Request(`https://${HOST}${path}`, {
    method: "POST",
    headers: {
      host: HOST,
      origin: `https://${HOST}`,
      "content-type": "application/x-www-form-urlencoded",
      ...headers,
    },
    body: new URLSearchParams(fields).toString(),
  });
}

describe("logout revokes the session token", () => {
  it("makes the cookie token unusable afterwards, including on login", async () => {
    const revocations = new RevocationList();
    const deps = { config: prod, limiter: new SlidingWindowRateLimiter(50, 60_000), revocations };
    const token = await prodToken();
    const cookie = `hitl_session=${token}`;

    expect((await handleLogin(form("/api/auth/login", { token }), deps)).headers.get("set-cookie")).toMatch(
      /^hitl_session=.+/,
    );

    const response = await handleLogout(form("/api/auth/logout", {}, { cookie }), deps);
    expect(response.headers.get("set-cookie")).toMatch(/Max-Age=0/);

    const relogin = await handleLogin(form("/api/auth/login", { token }), deps);
    expect(relogin.headers.get("location")).toContain("/login?error=invalid");
    expect(relogin.headers.get("set-cookie")).toBeNull();
    expect(await verifyOperatorToken(token, prod, new Date(), revocations)).toEqual({
      ok: false,
      code: "revoked",
    });
  });

  it("also revokes a Bearer-injected token, and authenticate() then denies it", async () => {
    const revocations = new RevocationList();
    const deps = { config: prod, limiter: new SlidingWindowRateLimiter(50, 60_000), revocations };
    const token = await prodToken();
    const request = () =>
      new Request(`https://${HOST}/x`, { headers: { authorization: `Bearer ${token}`, host: HOST } });
    expect((await authenticate(request(), prod, revocations)).ok).toBe(true);
    await handleLogout(form("/api/auth/logout", {}, { authorization: `Bearer ${token}` }), deps);
    expect(await authenticate(request(), prod, revocations)).toEqual({ ok: false, code: "revoked" });
  });

  it("uses the process-wide list by default so logout also affects authenticate()", async () => {
    const deps = { config: prod, limiter: new SlidingWindowRateLimiter(50, 60_000) };
    const token = await prodToken();
    const request = () =>
      new Request(`https://${HOST}/x`, { headers: { cookie: `hitl_session=${token}`, host: HOST } });
    expect((await authenticate(request(), prod)).ok).toBe(true);
    await handleLogout(form("/api/auth/logout", {}, { cookie: `hitl_session=${token}` }), deps);
    expect(await authenticate(request(), prod)).toEqual({ ok: false, code: "revoked" });
  });

  it("still clears the cookie when the token is missing or invalid", async () => {
    const deps = { config: prod, limiter: new SlidingWindowRateLimiter(50, 60_000), revocations: new RevocationList() };
    for (const headers of [{}, { cookie: "hitl_session=garbage" }] as Record<string, string>[]) {
      const response = await handleLogout(form("/api/auth/logout", {}, headers), deps);
      expect(response.status).toBe(303);
      expect(response.headers.get("set-cookie")).toMatch(/Max-Age=0/);
    }
  });
});

describe("backend URL transport", () => {
  const base = {
    HITL_JWT_SECRET: TEST_SECRET,
    HITL_JWT_ISSUER: ISSUER,
    HITL_JWT_AUDIENCE: AUDIENCE,
    NODE_ENV: "production",
  };

  it("refuses a plaintext http: backend URL in production", () => {
    expect(() => loadConfig({ ...base, HITL_API_BASE_URL: "http://backend.internal:4000" })).toThrow(
      /HITL_API_BASE_URL must use https/,
    );
    expect(() => loadConfig({ ...base, HITL_API_BASE_URL: "http://10.0.0.5:4000" })).toThrow(ConfigError);
    // a look-alike host that merely starts with a loopback name is not loopback
    expect(() => loadConfig({ ...base, HITL_API_BASE_URL: "http://localhost.evil.test:4000" })).toThrow(ConfigError);
    expect(() => loadConfig({ ...base, HITL_API_BASE_URL: "http://127.0.0.1.evil.test" })).toThrow(ConfigError);
  });

  it("accepts https, and http only for loopback hosts", () => {
    expect(loadConfig({ ...base, HITL_API_BASE_URL: "https://backend.internal:4000" }).apiBaseUrl).toBe(
      "https://backend.internal:4000",
    );
    for (const url of ["http://localhost:4000", "http://127.0.0.1:4000", "http://127.9.8.7", "http://[::1]:4000"]) {
      expect(loadConfig({ ...base, HITL_API_BASE_URL: url }).apiBaseUrl).not.toBeNull();
    }
  });

  it("still allows http backends outside production", () => {
    const cfg = loadConfig({ ...base, NODE_ENV: "development", HITL_API_BASE_URL: "http://backend.internal:4000" });
    expect(cfg.apiBaseUrl).toBe("http://backend.internal:4000");
  });
});
