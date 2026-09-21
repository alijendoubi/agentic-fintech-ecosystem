import { describe, expect, it } from "vitest";
import { deriveStatus, evaluateDecision, requiredApprovals } from "@/lib/signals/policy";
import type { Approval } from "@/lib/signals/schema";
import { APPROVER_A, APPROVER_B, GOOD_REASON, NOW_NS, VIEWER, makeSignal, nanos, nsFromNow } from "./helpers/fixtures";

const FOUR_EYES = { quantityThreshold: 1000, notionalThresholdUsd: null };

function decide(
  signal = makeSignal(),
  actor: Parameters<typeof evaluateDecision>[0]["actor"] = APPROVER_A,
  decision: "APPROVE" | "REJECT" = "APPROVE",
  reason = GOOD_REASON,
  fourEyes: { quantityThreshold: number; notionalThresholdUsd: number | null } = FOUR_EYES,
) {
  return evaluateDecision({ signal, actor, decision, reason, nowNs: NOW_NS, fourEyes });
}

function approvalBy(sub: string): Approval {
  return { approverSub: sub, decision: "APPROVE", reason: GOOD_REASON, decidedAtNs: nsFromNow(-1000) };
}

describe("requiredApprovals (four-eyes threshold)", () => {
  it("needs one approver below the quantity threshold", () => {
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(999) }), FOUR_EYES)).toBe(1);
  });
  it("needs two approvers at or above the quantity threshold", () => {
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(1000) }), FOUR_EYES)).toBe(2);
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(5000) }), FOUR_EYES)).toBe(2);
  });
  it("is exact at the one-nano boundary", () => {
    expect(requiredApprovals(makeSignal({ quantityNanos: "999999999999" }), FOUR_EYES)).toBe(1);
    expect(requiredApprovals(makeSignal({ quantityNanos: "1000000000000" }), FOUR_EYES)).toBe(2);
  });
  it("threshold 0 makes every signal four-eyes", () => {
    const all = { quantityThreshold: 0, notionalThresholdUsd: null };
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(1) }), all)).toBe(2);
  });
  it("applies the notional threshold to limit orders only", () => {
    const cfg = { quantityThreshold: 1000000, notionalThresholdUsd: 50000 };
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(100), priceLimitNanos: nanos(600) }), cfg)).toBe(2);
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(100), priceLimitNanos: nanos(400) }), cfg)).toBe(1);
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(100), priceLimitNanos: "0" }), cfg)).toBe(1);
  });
  it("never goes below what the backend requires", () => {
    expect(requiredApprovals(makeSignal({ quantityNanos: nanos(1), requiredApprovals: 2 }), FOUR_EYES)).toBe(2);
  });
});

describe("deriveStatus", () => {
  it("reports expired for a pending signal past valid_until", () => {
    expect(deriveStatus(makeSignal({ validUntilNs: nsFromNow(-1) }), NOW_NS)).toBe("EXPIRED");
  });
  it("reports expired when only the Aegis hold window has elapsed", () => {
    expect(deriveStatus(makeSignal({ holdExpiresAtNs: nsFromNow(-1) }), NOW_NS)).toBe("EXPIRED");
  });
  it("treats expiry as exclusive (expired exactly at the boundary)", () => {
    expect(deriveStatus(makeSignal({ validUntilNs: NOW_NS.toString() }), NOW_NS)).toBe("EXPIRED");
  });
  it("reports awaiting-second-approver after one approval of two", () => {
    const signal = makeSignal({ requiredApprovals: 2, approvals: [approvalBy("approver-a")] });
    expect(deriveStatus(signal, NOW_NS)).toBe("AWAITING_SECOND_APPROVER");
  });
  it("keeps terminal statuses", () => {
    expect(deriveStatus(makeSignal({ hitlStatus: "APPROVED" }), NOW_NS)).toBe("APPROVED");
    expect(deriveStatus(makeSignal({ hitlStatus: "REJECTED" }), NOW_NS)).toBe("REJECTED");
    expect(deriveStatus(makeSignal({ hitlStatus: "RELEASE_DENIED" }), NOW_NS)).toBe("RELEASE_DENIED");
  });
});

describe("evaluateDecision", () => {
  it("allows a single approval for a small signal and finalizes it", () => {
    expect(decide()).toEqual({ allowed: true, required: 1, finalizes: true });
  });

  it("denies viewers", () => {
    expect(decide(makeSignal(), VIEWER)).toMatchObject({ allowed: false, code: "forbidden" });
  });

  it("denies approvers without MFA evidence", () => {
    expect(decide(makeSignal(), { ...APPROVER_A, amr: ["pwd"] })).toMatchObject({ allowed: false, code: "forbidden" });
  });

  it.each(["", "   ", "short", "\n\n\n\n\n\n\n\n\n\n\n"])("denies a missing or too-short reason %j", (reason) => {
    expect(decide(makeSignal(), APPROVER_A, "APPROVE", reason)).toMatchObject({ allowed: false, code: "reason_required" });
    expect(decide(makeSignal(), APPROVER_A, "REJECT", reason)).toMatchObject({ allowed: false, code: "reason_required" });
  });

  it("denies approving an expired signal", () => {
    expect(decide(makeSignal({ validUntilNs: nsFromNow(-1000) }))).toMatchObject({ allowed: false, code: "expired" });
  });

  it("denies acting on an already-approved signal", () => {
    expect(decide(makeSignal({ hitlStatus: "APPROVED" }))).toMatchObject({ allowed: false, code: "already_final" });
  });

  it("reject is final: nothing can be done to a rejected signal", () => {
    const rejected = makeSignal({ hitlStatus: "REJECTED" });
    expect(decide(rejected, APPROVER_B, "APPROVE")).toMatchObject({ allowed: false, code: "already_final" });
    expect(decide(rejected, APPROVER_B, "REJECT")).toMatchObject({ allowed: false, code: "already_final" });
  });

  it("first approval on a four-eyes signal does not finalize", () => {
    expect(decide(makeSignal({ quantityNanos: nanos(5000) }))).toEqual({ allowed: true, required: 2, finalizes: false });
  });

  it("second distinct approver finalizes a four-eyes signal", () => {
    const signal = makeSignal({ quantityNanos: nanos(5000), approvals: [approvalBy("approver-a")] });
    expect(decide(signal, APPROVER_B)).toEqual({ allowed: true, required: 2, finalizes: true });
  });

  it("the same person cannot approve twice", () => {
    const signal = makeSignal({ quantityNanos: nanos(5000), approvals: [approvalBy("approver-a")] });
    expect(decide(signal, APPROVER_A)).toMatchObject({ allowed: false, code: "duplicate_approver" });
  });

  it("an approver who already approved may still reject (deny is always available)", () => {
    const signal = makeSignal({ quantityNanos: nanos(5000), approvals: [approvalBy("approver-a")] });
    expect(decide(signal, APPROVER_A, "REJECT")).toEqual({ allowed: true, required: 2, finalizes: true });
  });

  it("a reject finalizes immediately even on a four-eyes signal", () => {
    const signal = makeSignal({ quantityNanos: nanos(5000) });
    expect(decide(signal, APPROVER_A, "REJECT")).toMatchObject({ allowed: true, finalizes: true });
  });
});
