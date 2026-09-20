import type { Signal } from "@/lib/signals/schema";

export const NOW_MS = Date.UTC(2026, 8, 20, 12, 0, 0);
export const NOW_NS = BigInt(NOW_MS) * 1_000_000n;

export function nsFromNow(offsetMs: number): string {
  return (BigInt(NOW_MS + offsetMs) * 1_000_000n).toString();
}

/** Nanos string from a whole/decimal unit amount, e.g. nanos(100) === "100000000000". */
export function nanos(units: number): string {
  return BigInt(Math.round(units * 1e9)).toString();
}

export function makeSignal(overrides: Partial<Signal> = {}): Signal {
  return {
    holdId: "hold-test-001",
    signalId: "sig-test-001",
    symbol: "TESTCO",
    createdAtNs: nsFromNow(-60_000),
    side: "BUY",
    quantityNanos: nanos(100),
    priceLimitNanos: "0",
    estimatedSpreadCostNanos: nanos(1.5),
    estimatedMarketImpactNanos: nanos(2.5),
    estimatedVenueFeesNanos: nanos(0.5),
    estimatedTotalCostNanos: nanos(4.5),
    omega: 0.82,
    expectedValue: 124.5,
    pSuccess: 0.61,
    pFailure: 0.39,
    rewardEstimate: 300,
    riskEstimate: 150,
    regime: "TRENDING_BULL",
    regimeConfidence: 0.77,
    strategyId: "TEST-STRATEGY",
    debateSummary: "TEST FIXTURE judge synthesis.",
    debate: { blue: "TEST blue case", red: "TEST red case", judge: "TEST judge verdict" },
    validUntilNs: nsFromNow(10 * 60_000),
    holdExpiresAtNs: nsFromNow(10 * 60_000),
    heldReasons: ["REASON_UNUSUAL_ORDER_SIZE"],
    controls: [
      {
        controlId: "C19",
        isHard: false,
        passed: false,
        reason: "REASON_UNUSUAL_ORDER_SIZE",
        threshold: "P95",
        observed: "P97",
        detail: "TEST fixture",
      },
    ],
    hitlStatus: "PENDING",
    requiredApprovals: 1,
    approvals: [],
    ...overrides,
  };
}

export const APPROVER_A = { sub: "approver-a", role: "approver", amr: ["pwd", "mfa"] } as const;
export const APPROVER_B = { sub: "approver-b", role: "approver", amr: ["pwd", "mfa"] } as const;
export const VIEWER = { sub: "viewer-1", role: "viewer", amr: ["pwd", "mfa"] } as const;

export const GOOD_REASON = "Reviewed debate trace, sizing within limits.";
