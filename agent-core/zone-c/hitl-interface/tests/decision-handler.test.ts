import { describe, expect, it } from "vitest";
import { MockHitlApiClient } from "@/lib/api/mock-client";
import { deriveCsrfToken } from "@/lib/security/csrf";
import { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";
import { handleDecisionRequest } from "@/server/decision-handler";
import { GOOD_REASON, NOW_MS, makeSignal, nanos } from "./helpers/fixtures";
import { mintToken, testConfig } from "./helpers/tokens";

const config = testConfig();
const HOST = "hitl.test";

interface Opts {
  token?: string | null;
  csrf?: string | null;
  origin?: string | null;
  body?: unknown;
  contentType?: string | null;
  bearer?: boolean;
}

async function call(opts: Opts, client: MockHitlApiClient, limiter = new SlidingWindowRateLimiter(10, 60_000), holdId = "hold-test-001") {
  const token = opts.token === undefined ? await mintToken() : opts.token;
  const headers = new Headers({ host: HOST });
  if (opts.origin !== null) headers.set("origin", opts.origin ?? `https://${HOST}`);
  if (opts.contentType !== null) headers.set("content-type", opts.contentType ?? "application/json");
  if (token) {
    if (opts.bearer) headers.set("authorization", `Bearer ${token}`);
    else headers.set("cookie", `hitl_session=${token}`);
    const csrf = opts.csrf === undefined ? deriveCsrfToken(config.jwtSecret, token) : opts.csrf;
    if (csrf) headers.set("x-csrf-token", csrf);
  }
  const body = opts.body === undefined ? { decision: "approve", reason: GOOD_REASON } : opts.body;
  const request = new Request(`https://${HOST}/api/holds/${holdId}/decision`, {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
  const response = await handleDecisionRequest(request, holdId, { config, client, limiter, now: () => NOW_MS });
  return { status: response.status, json: (await response.json()) as Record<string, unknown> };
}

function newClient(signals = [makeSignal()]) {
  return new MockHitlApiClient({ signals, fourEyes: config.fourEyes, now: () => NOW_MS });
}

describe("decision route: happy path", () => {
  it("approves via cookie session with a valid CSRF token", async () => {
    const client = newClient();
    const { status, json } = await call({}, client);
    expect(status).toBe(200);
    expect(json).toMatchObject({ ok: true });
    expect(client.auditLog()).toHaveLength(1);
  });

  it("also accepts a gateway-injected Bearer token", async () => {
    const { status } = await call({ bearer: true, token: await mintToken() }, newClient());
    expect(status).toBe(200);
  });

  it("rejects with a reason", async () => {
    const { status, json } = await call({ body: { decision: "reject", reason: "Regime looks wrong for this size." } }, newClient());
    expect(status).toBe(200);
    expect((json.signal as { hitlStatus: string }).hitlStatus).toBe("REJECTED");
  });
});

describe("decision route: CSRF, origin, auth", () => {
  it.each([
    ["missing origin", { origin: null }],
    ["foreign origin", { origin: "https://evil.test" }],
  ])("denies %s and records nothing", async (_name, opts) => {
    const client = newClient();
    const { status, json } = await call(opts, client);
    expect(status).toBe(403);
    expect(json).toMatchObject({ ok: false, code: "csrf", denied: true });
    expect(client.auditLog()).toHaveLength(0);
  });

  it("denies a missing or wrong CSRF token", async () => {
    expect((await call({ csrf: null }, newClient())).status).toBe(403);
    expect((await call({ csrf: "deadbeef" }, newClient())).status).toBe(403);
  });

  it("denies a CSRF token minted for a different session", async () => {
    const other = await mintToken({ sub: "someone-else" });
    expect((await call({ csrf: deriveCsrfToken(config.jwtSecret, other) }, newClient())).status).toBe(403);
  });

  it("denies unauthenticated, expired, and no-MFA sessions with 401", async () => {
    expect((await call({ token: null }, newClient())).status).toBe(401);
    const expired = await mintToken({ expiresInSec: 60, issuedAt: new Date(Date.now() - 3_600_000) });
    expect((await call({ token: expired }, newClient())).status).toBe(401);
    expect((await call({ token: await mintToken({ amr: ["pwd"] }) }, newClient())).status).toBe(401);
  });

  it("denies a viewer with 403 and records nothing", async () => {
    const client = newClient();
    const { status, json } = await call({ token: await mintToken({ role: "viewer", sub: "viewer-1" }) }, client);
    expect(status).toBe(403);
    expect(json).toMatchObject({ ok: false, code: "forbidden" });
    expect(client.auditLog()).toHaveLength(0);
  });
});

describe("decision route: validation and rate limit", () => {
  it.each([
    ["empty reason", { decision: "approve", reason: "" }],
    ["short reason", { decision: "approve", reason: "ok" }],
    ["missing reason", { decision: "reject" }],
    ["whitespace reason", { decision: "reject", reason: "          " }],
  ])("returns 422 for %s", async (_n, body) => {
    const client = newClient();
    const { status, json } = await call({ body }, client);
    expect(status).toBe(422);
    expect(json.code).toBe("reason_required");
    expect(client.auditLog()).toHaveLength(0);
  });

  it("returns 400 for unknown decision values, extra keys, non-JSON and wrong content type", async () => {
    expect((await call({ body: { decision: "maybe", reason: GOOD_REASON } }, newClient())).status).toBe(400);
    expect((await call({ body: { decision: "approve", reason: GOOD_REASON, approverSub: "spoof" } }, newClient())).status).toBe(400);
    expect((await call({ body: "{not json" }, newClient())).status).toBe(400);
    expect((await call({ contentType: "text/plain" }, newClient())).status).toBe(400);
  });

  it("rejects hold ids that are not simple identifiers", async () => {
    expect((await call({}, newClient(), undefined, "a..b%2F")).status).toBe(404);
  });

  it("rate limits repeated mutations per operator", async () => {
    const limiter = new SlidingWindowRateLimiter(2, 60_000, () => 0);
    const client = newClient([makeSignal({ quantityNanos: nanos(9000) })]);
    await call({}, client, limiter);
    await call({}, client, limiter);
    const third = await call({}, client, limiter);
    expect(third.status).toBe(429);
    expect(third.json.code).toBe("rate_limited");
  });
});

describe("decision route: business rules and deny on error", () => {
  it("returns 409 for an expired signal", async () => {
    const client = newClient([makeSignal({ validUntilNs: "1" })]);
    const { status, json } = await call({}, client);
    expect(status).toBe(409);
    expect(json.code).toBe("expired");
  });

  it("returns 409 when the same approver approves twice on a four-eyes signal", async () => {
    const client = newClient([makeSignal({ quantityNanos: nanos(5000) })]);
    expect((await call({}, client)).status).toBe(200);
    const again = await call({}, client);
    expect(again.status).toBe(409);
    expect(again.json.code).toBe("duplicate_approver");
  });

  it.each([
    ["timeout", 504],
    ["network", 502],
    ["server_error", 502],
  ] as const)("backend %s is denied with %i and states nothing was approved", async (code, expected) => {
    const client = newClient();
    client.setFailure({ code, message: "x" });
    const { status, json } = await call({}, client);
    expect(status).toBe(expected);
    expect(json).toMatchObject({ ok: false, denied: true });
    expect(String(json.message)).toMatch(/Nothing was approved|not found/);
  });
});
