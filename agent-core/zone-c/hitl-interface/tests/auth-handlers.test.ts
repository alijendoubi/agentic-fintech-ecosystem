import { describe, expect, it } from "vitest";
import { verifyOperatorToken } from "@/lib/auth/verify";
import { DEMO_PERSONAS, buildDemoSignals, createDemoClient, mintDemoToken } from "@/lib/demo";
import { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";
import { handleDemoLogin, handleLogin, handleLogout } from "@/server/auth-handlers";
import { clearedSessionCookie, readCookie, sessionCookie } from "@/server/http";
import { mintToken, testConfig } from "./helpers/tokens";

const HOST = "hitl.test";
const config = testConfig();
const demoConfig = testConfig({ HITL_DEMO_MODE: "true", HITL_API_BASE_URL: undefined });

function form(path: string, fields: Record<string, string>, origin: string | null = `https://${HOST}`): Request {
  const headers = new Headers({ host: HOST, "content-type": "application/x-www-form-urlencoded" });
  if (origin) headers.set("origin", origin);
  return new Request(`https://${HOST}${path}`, { method: "POST", headers, body: new URLSearchParams(fields).toString() });
}

const deps = (cfg = config, limit = 10) => ({ config: cfg, limiter: new SlidingWindowRateLimiter(limit, 60_000) });

describe("cookie helpers", () => {
  it("reads a named cookie among several", () => {
    expect(readCookie("a=1; hitl_session=tok; b=2", "hitl_session")).toBe("tok");
    expect(readCookie("a=1", "hitl_session")).toBeUndefined();
    expect(readCookie(null, "x")).toBeUndefined();
  });
  it("sets HttpOnly, SameSite=Strict and Secure in production", () => {
    const prod = sessionCookie("tok", 60, true);
    expect(prod).toMatch(/HttpOnly/);
    expect(prod).toMatch(/SameSite=Strict/);
    expect(prod).toMatch(/Secure/);
    expect(sessionCookie("tok", 60, false)).not.toMatch(/Secure/);
    expect(clearedSessionCookie(true)).toMatch(/Max-Age=0/);
  });
});

describe("login", () => {
  it("sets a session cookie for a valid token and redirects home", async () => {
    const response = await handleLogin(form("/api/auth/login", { token: await mintToken() }), deps());
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe(`https://${HOST}/`);
    expect(response.headers.get("set-cookie")).toMatch(/^hitl_session=.+HttpOnly/);
  });

  it.each([
    ["no MFA", () => mintToken({ amr: ["pwd"] })],
    ["bad signature", () => mintToken({ secret: "x".repeat(8) + "Qw3rTy9UiOp5AsDf1234567890ab" })],
    ["empty", async () => ""],
    ["garbage", async () => "abc"],
  ])("refuses a token with %s and sets no cookie", async (_name, make) => {
    const response = await handleLogin(form("/api/auth/login", { token: await make() }), deps());
    expect(response.headers.get("location")).toContain("/login?error=invalid");
    expect(response.headers.get("set-cookie")).toBeNull();
  });

  it("refuses cross-origin and rate-limited attempts", async () => {
    expect((await handleLogin(form("/api/auth/login", { token: "x" }, "https://evil.test"), deps())).status).toBe(403);
    expect((await handleLogin(form("/api/auth/login", { token: "x" }, null), deps())).status).toBe(403);
    const limited = deps(config, 1);
    await handleLogin(form("/api/auth/login", { token: "x" }), limited);
    expect((await handleLogin(form("/api/auth/login", { token: "x" }), limited)).status).toBe(429);
  });
});

describe("logout", () => {
  it("clears the cookie", async () => {
    const response = await handleLogout(form("/api/auth/logout", {}), deps());
    expect(response.headers.get("set-cookie")).toMatch(/Max-Age=0/);
  });
  it("refuses cross-origin logout", async () => {
    expect((await handleLogout(form("/api/auth/logout", {}, "https://evil.test"), deps())).status).toBe(403);
  });
});

describe("demo mode", () => {
  it("is unavailable when demo mode is off", async () => {
    const response = await handleDemoLogin(form("/api/auth/demo", { persona: "demo-approver-1" }), deps());
    expect(response.status).toBe(404);
  });

  it("signs in a synthetic persona whose token passes normal verification", async () => {
    const response = await handleDemoLogin(form("/api/auth/demo", { persona: "demo-approver-1" }), deps(demoConfig));
    expect(response.status).toBe(303);
    const token = /hitl_session=([^;]+)/.exec(response.headers.get("set-cookie") ?? "")?.[1];
    const verified = await verifyOperatorToken(token, demoConfig);
    expect(verified.ok && verified.session.sub).toBe("demo-approver-1");
  });

  it("refuses unknown personas", async () => {
    const response = await handleDemoLogin(form("/api/auth/demo", { persona: "root" }), deps(demoConfig));
    expect(response.headers.get("location")).toContain("error=invalid");
  });

  it("cannot mint tokens or build a client with demo mode off", async () => {
    await expect(mintDemoToken(config, "demo-approver-1", Date.now())).rejects.toThrow();
    expect(() => createDemoClient(config, Date.now())).toThrow();
  });

  it("provides synthetic personas and signals clearly marked DEMO", () => {
    expect(DEMO_PERSONAS.map((p) => p.id)).toEqual(["demo-approver-1", "demo-approver-2", "demo-viewer"]);
    for (const signal of buildDemoSignals(Date.now())) expect(signal.debateSummary).toMatch(/DEMO/);
  });
});
