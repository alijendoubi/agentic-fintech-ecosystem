import { describe, expect, it } from "vitest";
import { ConfigError, loadConfig } from "@/lib/config";
import { verifyOperatorToken } from "@/lib/auth/verify";
import { TEST_SECRET, mintToken, testConfig } from "./helpers/tokens";

const ISSUER = "https://idp.example.test";
const AUDIENCE = "afe-hitl";

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
