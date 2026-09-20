import { describe, expect, it } from "vitest";
import { MockHitlApiClient } from "@/lib/api/mock-client";
import type { ApiError, DecisionSubmission, HitlApiClient, OperatorContext, Result } from "@/lib/api/types";
import { submitOperatorDecision } from "@/lib/decision-service";
import type { OperatorSession } from "@/lib/auth/verify";
import type { Signal } from "@/lib/signals/schema";
import { GOOD_REASON, NOW_MS, makeSignal, nanos, nsFromNow } from "./helpers/fixtures";

const FOUR_EYES = { quantityThreshold: 1000, notionalThresholdUsd: null };

function session(sub: string, role: "approver" | "viewer" = "approver", amr: string[] = ["pwd", "mfa"]): OperatorSession {
  return { sub, role, amr, expiresAt: 0, token: `token-of-${sub}` };
}

function setup(signals: Signal[], failure?: ApiError) {
  const client = new MockHitlApiClient({ signals, fourEyes: FOUR_EYES, now: () => NOW_MS, failure });
  let n = 0;
  const deps = { client, fourEyes: FOUR_EYES, now: () => NOW_MS, newRequestId: () => `req-${++n}` };
  return { client, deps };
}

const approve = { decision: "approve", reason: GOOD_REASON } as const;
const reject = { decision: "reject", reason: GOOD_REASON } as const;

describe("submitOperatorDecision: approve and reject flows", () => {
  it("approves a small signal and records it in the backend audit trail", async () => {
    const { client, deps } = setup([makeSignal()]);
    const result = await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", approve);
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.signal.hitlStatus).toBe("APPROVED");
    expect(client.auditLog()).toHaveLength(1);
    expect(client.auditLog()[0]).toMatchObject({ actorSub: "approver-a", decision: "APPROVE", outcome: "recorded", reason: GOOD_REASON });
  });

  it("rejects a signal and reject is final", async () => {
    const { deps } = setup([makeSignal()]);
    const first = await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", reject);
    expect(first.ok && first.signal.hitlStatus).toBe("REJECTED");
    const again = await submitOperatorDecision(deps, session("approver-b"), "hold-test-001", approve);
    expect(again).toMatchObject({ ok: false, code: "already_final", denied: true });
  });

  it("denies a viewer without calling the backend", async () => {
    const { client, deps } = setup([makeSignal()]);
    const result = await submitOperatorDecision(deps, session("viewer-1", "viewer"), "hold-test-001", approve);
    expect(result).toMatchObject({ ok: false, code: "forbidden" });
    expect(client.auditLog()).toHaveLength(0);
  });

  it("denies a session without MFA", async () => {
    const { deps } = setup([makeSignal()]);
    const result = await submitOperatorDecision(deps, session("approver-a", "approver", ["pwd"]), "hold-test-001", approve);
    expect(result).toMatchObject({ ok: false, code: "forbidden" });
  });

  it("denies an expired signal", async () => {
    const { deps } = setup([makeSignal({ validUntilNs: nsFromNow(-1) })]);
    const result = await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", approve);
    expect(result).toMatchObject({ ok: false, code: "expired" });
  });

  it("denies an unknown hold", async () => {
    const { deps } = setup([]);
    const result = await submitOperatorDecision(deps, session("approver-a"), "nope", approve);
    expect(result).toMatchObject({ ok: false, code: "not_found" });
  });
});

describe("submitOperatorDecision: four-eyes", () => {
  const big = () => makeSignal({ quantityNanos: nanos(5000) });

  it("first approval leaves the signal pending, second distinct approver finalizes", async () => {
    const { deps } = setup([big()]);
    const first = await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", approve);
    expect(first.ok && first.signal.hitlStatus).toBe("PENDING");
    const second = await submitOperatorDecision(deps, session("approver-b"), "hold-test-001", approve);
    expect(second.ok && second.signal.hitlStatus).toBe("APPROVED");
    expect(second.ok && second.signal.approvals.map((a) => a.approverSub)).toEqual(["approver-a", "approver-b"]);
  });

  it("the same person cannot approve twice", async () => {
    const { deps } = setup([big()]);
    await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", approve);
    const again = await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", approve);
    expect(again).toMatchObject({ ok: false, code: "duplicate_approver" });
  });

  it("a reject by the second approver ends it", async () => {
    const { deps } = setup([big()]);
    await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", approve);
    const second = await submitOperatorDecision(deps, session("approver-b"), "hold-test-001", reject);
    expect(second.ok && second.signal.hitlStatus).toBe("REJECTED");
  });
});

describe("submitOperatorDecision: deny on error", () => {
  it.each([
    ["timeout", "timeout"],
    ["network", "backend_error"],
    ["server_error", "backend_error"],
    ["unavailable", "backend_error"],
    ["invalid_response", "invalid_response"],
    ["unauthorized", "unauthorized"],
    ["forbidden", "unauthorized"],
  ] as const)("backend failure %s is a denial (%s) and nothing is approved", async (apiCode, expected) => {
    const { deps } = setup([makeSignal()], { code: apiCode, message: "boom" });
    const result = await submitOperatorDecision(deps, session("approver-a"), "hold-test-001", approve);
    expect(result).toMatchObject({ ok: false, code: expected, denied: true });
  });

  it("a failure while submitting (after a successful read) is a denial", async () => {
    const base = new MockHitlApiClient({ signals: [makeSignal()], fourEyes: FOUR_EYES, now: () => NOW_MS });
    const flaky: HitlApiClient = {
      listHolds: (ctx: OperatorContext) => base.listHolds(ctx),
      getHold: (id: string, ctx: OperatorContext) => base.getHold(id, ctx),
      submitDecision: async (): Promise<Result<Signal>> => ({ ok: false, error: { code: "timeout", message: "t" } }),
    };
    const result = await submitOperatorDecision(
      { client: flaky, fourEyes: FOUR_EYES, now: () => NOW_MS },
      session("approver-a"),
      "hold-test-001",
      approve,
    );
    expect(result).toMatchObject({ ok: false, code: "timeout", denied: true });
    expect(result.ok === false && result.message).toMatch(/Nothing was approved/);
  });

  it("a backend answer that does not confirm the decision is a denial", async () => {
    const base = new MockHitlApiClient({ signals: [makeSignal()], fourEyes: FOUR_EYES, now: () => NOW_MS });
    const lying: HitlApiClient = {
      listHolds: (ctx: OperatorContext) => base.listHolds(ctx),
      getHold: (id: string, ctx: OperatorContext) => base.getHold(id, ctx),
      submitDecision: async (_s: DecisionSubmission): Promise<Result<Signal>> => ({ ok: true, value: makeSignal({ hitlStatus: "APPROVED" }) }),
    };
    const result = await submitOperatorDecision(
      { client: lying, fourEyes: FOUR_EYES, now: () => NOW_MS },
      session("approver-a"),
      "hold-test-001",
      approve,
    );
    expect(result).toMatchObject({ ok: false, code: "invalid_response" });
  });
});
