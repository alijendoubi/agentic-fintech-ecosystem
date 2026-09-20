import { SignJWT } from "jose";
import { describe, expect, it } from "vitest";
import { canAct, verifyOperatorToken } from "@/lib/auth/verify";
import { extractToken } from "@/lib/auth/identity";
import { TEST_SECRET, mintToken, testConfig } from "./helpers/tokens";

const cfg = testConfig();

describe("verifyOperatorToken", () => {
  it("accepts a valid approver token with MFA", async () => {
    const result = await verifyOperatorToken(await mintToken(), cfg);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.session.sub).toBe("approver-a");
      expect(result.session.role).toBe("approver");
      expect(canAct(result.session)).toBe(true);
    }
  });

  it("accepts a viewer token but the viewer cannot act", async () => {
    const result = await verifyOperatorToken(await mintToken({ role: "viewer" }), cfg);
    expect(result.ok).toBe(true);
    if (result.ok) expect(canAct(result.session)).toBe(false);
  });

  it.each([
    ["missing token", null, "missing_token"],
    ["empty token", "", "missing_token"],
    ["garbage token", "not.a.jwt", "invalid_token"],
  ])("denies %s", async (_name, token, code) => {
    const result = await verifyOperatorToken(token, cfg);
    expect(result).toEqual({ ok: false, code });
  });

  it("denies an expired token", async () => {
    const token = await mintToken({ expiresInSec: 60, issuedAt: new Date(Date.now() - 3_600_000) });
    expect(await verifyOperatorToken(token, cfg)).toEqual({ ok: false, code: "expired" });
  });

  it("denies a token signed with the wrong secret", async () => {
    const token = await mintToken({ secret: "z".repeat(16) + "Qw3rTy9UiOp5AsDf" });
    expect(await verifyOperatorToken(token, cfg)).toEqual({ ok: false, code: "bad_signature" });
  });

  it("denies alg=none tokens", async () => {
    const header = Buffer.from(JSON.stringify({ alg: "none", typ: "JWT" })).toString("base64url");
    const payload = Buffer.from(
      JSON.stringify({ sub: "x", role: "approver", amr: ["mfa"], exp: Math.floor(Date.now() / 1000) + 600 }),
    ).toString("base64url");
    const result = await verifyOperatorToken(`${header}.${payload}.`, cfg);
    expect(result.ok).toBe(false);
  });

  it("denies tokens using a different HMAC algorithm than HS256", async () => {
    const result = await verifyOperatorToken(await mintToken({ alg: "HS512" }), cfg);
    expect(result.ok).toBe(false);
  });

  it("denies a token without MFA evidence", async () => {
    expect(await verifyOperatorToken(await mintToken({ amr: ["pwd"] }), cfg)).toEqual({
      ok: false,
      code: "mfa_required",
    });
    expect(await verifyOperatorToken(await mintToken({ amr: null }), cfg)).toEqual({
      ok: false,
      code: "mfa_required",
    });
    expect(await verifyOperatorToken(await mintToken({ amr: [] }), cfg)).toEqual({
      ok: false,
      code: "mfa_required",
    });
  });

  it("denies a token without a subject or with a blank subject", async () => {
    expect(await verifyOperatorToken(await mintToken({ sub: null }), cfg)).toEqual({
      ok: false,
      code: "missing_subject",
    });
    expect(await verifyOperatorToken(await mintToken({ sub: "   " }), cfg)).toEqual({
      ok: false,
      code: "missing_subject",
    });
  });

  it("denies unknown or missing roles", async () => {
    expect(await verifyOperatorToken(await mintToken({ role: "admin" }), cfg)).toEqual({
      ok: false,
      code: "invalid_role",
    });
    expect(await verifyOperatorToken(await mintToken({ role: null }), cfg)).toEqual({
      ok: false,
      code: "invalid_role",
    });
  });

  it("denies a token with no expiry claim", async () => {
    const token = await new SignJWT({ role: "approver", amr: ["mfa"] })
      .setProtectedHeader({ alg: "HS256" })
      .setSubject("approver-a")
      .sign(new TextEncoder().encode(TEST_SECRET));
    expect((await verifyOperatorToken(token, cfg)).ok).toBe(false);
  });

  it("enforces issuer and audience when configured", async () => {
    const strict = testConfig({ HITL_JWT_ISSUER: "https://idp.example.test", HITL_JWT_AUDIENCE: "afe-hitl" });
    const good = await mintToken({ issuer: "https://idp.example.test", audience: "afe-hitl" });
    expect((await verifyOperatorToken(good, strict)).ok).toBe(true);
    expect((await verifyOperatorToken(await mintToken(), strict)).ok).toBe(false);
    expect(
      (await verifyOperatorToken(await mintToken({ issuer: "https://evil.test", audience: "afe-hitl" }), strict)).ok,
    ).toBe(false);
  });
});

describe("canAct", () => {
  const base = { sub: "a", expiresAt: 1, token: "t" } as const;
  it("requires both approver role and MFA", () => {
    expect(canAct({ ...base, role: "approver", amr: ["mfa"] })).toBe(true);
    expect(canAct({ ...base, role: "approver", amr: ["pwd"] })).toBe(false);
    expect(canAct({ ...base, role: "viewer", amr: ["mfa"] })).toBe(false);
  });
});

describe("extractToken (identity adapter boundary)", () => {
  it("prefers a Bearer Authorization header over the cookie", () => {
    expect(extractToken({ authorization: "Bearer abc", cookie: "xyz" })).toBe("abc");
  });
  it("falls back to the session cookie", () => {
    expect(extractToken({ authorization: null, cookie: "xyz" })).toBe("xyz");
  });
  it("ignores non-Bearer authorization schemes", () => {
    expect(extractToken({ authorization: "Basic abc", cookie: undefined })).toBeNull();
  });
  it("returns null when nothing is supplied", () => {
    expect(extractToken({ authorization: null, cookie: undefined })).toBeNull();
  });
});
