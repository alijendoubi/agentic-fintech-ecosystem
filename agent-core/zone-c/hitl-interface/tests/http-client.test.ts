import { describe, expect, it, vi } from "vitest";
import { HttpHitlApiClient } from "@/lib/api/http-client";
import { makeSignal } from "./helpers/fixtures";

const CTX = { token: "jwt-token", sub: "approver-a" };
const SUBMISSION = {
  holdId: "hold-test-001",
  decision: "APPROVE",
  reason: "Reviewed debate trace, sizing within limits.",
  approver: { sub: "approver-a", role: "approver", amr: ["mfa"] },
  clientRequestId: "req-1",
} as const;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function client(fetchImpl: typeof fetch, extra: { serviceToken?: string } = {}) {
  return new HttpHitlApiClient({ baseUrl: "http://backend.test:4000/", timeoutMs: 50, fetchImpl, ...extra });
}

describe("HttpHitlApiClient", () => {
  it("lists holds with bearer auth and validates the shape", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ holds: [makeSignal()] }));
    const result = await client(fetchImpl as unknown as typeof fetch, { serviceToken: "svc" }).listHolds(CTX);
    expect(result.ok && result.value).toHaveLength(1);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://backend.test:4000/v1/holds?status=pending");
    expect(init.headers).toMatchObject({ authorization: "Bearer jwt-token", "x-service-token": "svc" });
  });

  it("posts the decision as JSON to the hold's decisions endpoint", async () => {
    const decided = makeSignal({ hitlStatus: "APPROVED" });
    const fetchImpl = vi.fn(async () => jsonResponse(decided));
    const result = await client(fetchImpl as unknown as typeof fetch).submitDecision(SUBMISSION, CTX);
    expect(result.ok).toBe(true);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://backend.test:4000/v1/holds/hold-test-001/decisions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toMatchObject({ decision: "APPROVE", clientRequestId: "req-1" });
  });

  it.each([
    [401, "unauthorized"],
    [403, "forbidden"],
    [404, "not_found"],
    [409, "refused"],
    [410, "refused"],
    [422, "refused"],
    [429, "unavailable"],
    [503, "unavailable"],
    [500, "server_error"],
    [418, "server_error"],
  ] as const)("maps HTTP %i to %s", async (status, code) => {
    const fetchImpl = vi.fn(async () => jsonResponse({ error: { code: "x", message: "denied by backend" } }, status));
    const result = await client(fetchImpl as unknown as typeof fetch).getHold("hold-test-001", CTX);
    expect(result).toMatchObject({ ok: false, error: { code } });
  });

  it("treats a schema violation as invalid_response", async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ holdId: "hold-test-001" }));
    const result = await client(fetchImpl as unknown as typeof fetch).getHold("hold-test-001", CTX);
    expect(result).toMatchObject({ ok: false, error: { code: "invalid_response" } });
  });

  it("treats float money on the wire as invalid_response", async () => {
    const floaty = { ...makeSignal(), quantityNanos: 100.5 };
    const fetchImpl = vi.fn(async () => jsonResponse(floaty));
    const result = await client(fetchImpl as unknown as typeof fetch).getHold("hold-test-001", CTX);
    expect(result).toMatchObject({ ok: false, error: { code: "invalid_response" } });
  });

  it("treats a non-JSON body as invalid_response", async () => {
    const fetchImpl = vi.fn(async () => new Response("<html>", { status: 200 }));
    const result = await client(fetchImpl as unknown as typeof fetch).getHold("hold-test-001", CTX);
    expect(result).toMatchObject({ ok: false, error: { code: "invalid_response" } });
  });

  it("maps network errors to network", async () => {
    const fetchImpl = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    const result = await client(fetchImpl as unknown as typeof fetch).getHold("hold-test-001", CTX);
    expect(result).toMatchObject({ ok: false, error: { code: "network" } });
  });

  it("maps a slow backend to timeout", async () => {
    const fetchImpl = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(init.signal?.reason));
        }),
    );
    const result = await client(fetchImpl as unknown as typeof fetch).getHold("hold-test-001", CTX);
    expect(result).toMatchObject({ ok: false, error: { code: "timeout" } });
  });

  it("rejects path-traversal style hold ids without calling the backend", async () => {
    const fetchImpl = vi.fn();
    const c = client(fetchImpl as unknown as typeof fetch);
    expect(await c.getHold("../admin", CTX)).toMatchObject({ ok: false, error: { code: "not_found" } });
    expect(await c.submitDecision({ ...SUBMISSION, holdId: "a/b" }, CTX)).toMatchObject({ ok: false });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
