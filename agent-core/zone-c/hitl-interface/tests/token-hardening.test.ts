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
    const token = await mintToken({ issuer: ISSUER, audience: AUDIENCE });
    expect((await verifyOperatorToken(token, prod)).ok).toBe(true);
  });

  it("denies a token without issuer or audience", async () => {
    const noIss = await mintToken({ audience: AUDIENCE });
    const noAud = await mintToken({ issuer: ISSUER });
    expect((await verifyOperatorToken(noIss, prod)).ok).toBe(false);
    expect((await verifyOperatorToken(noAud, prod)).ok).toBe(false);
  });

  it("fails closed if a production verifier is somehow built without issuer/audience", async () => {
    const weak = { ...prod, jwtIssuer: null, jwtAudience: null };
    const token = await mintToken({ issuer: ISSUER, audience: AUDIENCE });
    expect((await verifyOperatorToken(token, weak)).ok).toBe(false);
  });
});
