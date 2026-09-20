import { SignJWT } from "jose";
import type { AppConfig } from "@/lib/config";
import { MockHitlApiClient } from "@/lib/api/mock-client";
import type { Signal } from "@/lib/signals/schema";

/**
 * DEMO MODE ONLY (HITL_DEMO_MODE=true, refused when NODE_ENV=production).
 * Personas are generic placeholders, not real people. Tokens are minted with
 * the configured HITL_JWT_SECRET so they pass the same verification as real ones.
 * Everything here is synthetic; none of it reflects real signals or approvals.
 */

export const DEMO_PERSONAS = [
  { id: "demo-approver-1", role: "approver" },
  { id: "demo-approver-2", role: "approver" },
  { id: "demo-viewer", role: "viewer" },
] as const;

export type DemoPersonaId = (typeof DEMO_PERSONAS)[number]["id"];

const DEMO_TOKEN_TTL_SEC = 3600;

export function isDemoPersona(value: string): value is DemoPersonaId {
  return DEMO_PERSONAS.some((persona) => persona.id === value);
}

export async function mintDemoToken(config: AppConfig, personaId: DemoPersonaId, nowMs: number): Promise<string> {
  if (!config.demoMode || config.isProduction) {
    throw new Error("Demo tokens are only available in demo mode outside production");
  }
  const persona = DEMO_PERSONAS.find((p) => p.id === personaId);
  if (!persona) throw new Error("Unknown demo persona");
  const iat = Math.floor(nowMs / 1000);
  return new SignJWT({ role: persona.role, amr: ["pwd", "mfa"] })
    .setProtectedHeader({ alg: "HS256" })
    .setSubject(persona.id)
    .setIssuedAt(iat)
    .setExpirationTime(iat + DEMO_TOKEN_TTL_SEC)
    .sign(new TextEncoder().encode(config.jwtSecret));
}

function nanos(units: number): string {
  return BigInt(Math.round(units * 1e9)).toString();
}

function nsAt(ms: number): string {
  return (BigInt(Math.trunc(ms)) * 1_000_000n).toString();
}

export function buildDemoSignals(nowMs: number): readonly Signal[] {
  const minute = 60_000;
  const base = {
    createdAtNs: nsAt(nowMs - minute),
    omega: 0.71,
    pFailure: 0.41,
    pSuccess: 0.59,
    regimeConfidence: 0.66,
    strategyId: "DEMO-STRATEGY",
    approvals: [],
    hitlStatus: "PENDING" as const,
    requiredApprovals: 1,
  };
  const debate = (symbol: string) => ({
    blue: `DEMO: bull case for ${symbol} (synthetic text).`,
    red: `DEMO: bear case for ${symbol} (synthetic text).`,
    judge: `DEMO: judge weighs both cases for ${symbol} (synthetic text).`,
  });
  const control = {
    controlId: "C19",
    isHard: false,
    passed: false,
    reason: "REASON_UNUSUAL_ORDER_SIZE",
    threshold: "P95 of trailing 30d",
    observed: "DEMO",
    detail: "DEMO: synthetic control result",
  };
  return [
    {
      ...base,
      holdId: "demo-hold-001",
      signalId: "demo-signal-001",
      symbol: "DEMO1",
      side: "BUY",
      quantityNanos: nanos(200),
      priceLimitNanos: nanos(50),
      estimatedSpreadCostNanos: nanos(1),
      estimatedMarketImpactNanos: nanos(2),
      estimatedVenueFeesNanos: nanos(0.5),
      estimatedTotalCostNanos: nanos(3.5),
      expectedValue: 42,
      rewardEstimate: 120,
      riskEstimate: 60,
      regime: "TRENDING_BULL",
      debateSummary: "DEMO: synthetic judge synthesis for a small order.",
      debate: debate("DEMO1"),
      validUntilNs: nsAt(nowMs + 30 * minute),
      holdExpiresAtNs: nsAt(nowMs + 30 * minute),
      heldReasons: [control.reason],
      controls: [control],
    },
    {
      ...base,
      holdId: "demo-hold-002",
      signalId: "demo-signal-002",
      symbol: "DEMO2",
      side: "SELL",
      quantityNanos: nanos(5000),
      priceLimitNanos: nanos(20),
      estimatedSpreadCostNanos: nanos(5),
      estimatedMarketImpactNanos: nanos(12),
      estimatedVenueFeesNanos: nanos(3),
      estimatedTotalCostNanos: nanos(20),
      expectedValue: 310,
      rewardEstimate: 900,
      riskEstimate: 450,
      regime: "HIGH_VOL_CHOP",
      debateSummary: "DEMO: synthetic judge synthesis for a large (four-eyes) order.",
      debate: debate("DEMO2"),
      validUntilNs: nsAt(nowMs + 45 * minute),
      holdExpiresAtNs: nsAt(nowMs + 45 * minute),
      heldReasons: [control.reason],
      controls: [control],
    },
    {
      ...base,
      holdId: "demo-hold-003",
      signalId: "demo-signal-003",
      symbol: "DEMO3",
      side: "BUY",
      quantityNanos: nanos(50),
      priceLimitNanos: "0",
      estimatedSpreadCostNanos: nanos(0.2),
      estimatedMarketImpactNanos: nanos(0.3),
      estimatedVenueFeesNanos: nanos(0.1),
      estimatedTotalCostNanos: nanos(0.6),
      expectedValue: 5,
      rewardEstimate: 20,
      riskEstimate: 15,
      regime: "LOW_VOL_CHOP",
      debateSummary: "DEMO: synthetic signal that is about to expire.",
      debate: debate("DEMO3"),
      validUntilNs: nsAt(nowMs + 90_000),
      holdExpiresAtNs: nsAt(nowMs + 90_000),
      heldReasons: ["REASON_REGIME_LOW_CONFIDENCE"],
      controls: [{ ...control, controlId: "C18", reason: "REASON_REGIME_LOW_CONFIDENCE" }],
    },
  ];
}

export function createDemoClient(config: AppConfig, nowMs: number): MockHitlApiClient {
  if (!config.demoMode || config.isProduction) {
    throw new Error("Demo client is only available in demo mode outside production");
  }
  return new MockHitlApiClient({ signals: buildDemoSignals(nowMs), fourEyes: config.fourEyes });
}
