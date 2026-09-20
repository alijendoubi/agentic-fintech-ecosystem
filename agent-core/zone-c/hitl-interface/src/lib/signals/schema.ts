import { z } from "zod";

/**
 * Wire schemas for the assumed backend contract (docs/api-contract.md).
 * Field names follow proto3 canonical JSON of shared/proto/trade_signal.proto and
 * aegis.proto (lowerCamelCase, int64 as decimal string, enums as names).
 * Money, price and quantity are int64 nanos (1e-9) strings, never floats.
 */

export const SIDES = ["BUY", "SELL", "SELL_SHORT"] as const;
export const REGIMES = [
  "REGIME_UNKNOWN",
  "TRENDING_BULL",
  "TRENDING_BEAR",
  "HIGH_VOL_CHOP",
  "LOW_VOL_CHOP",
  "CRISIS",
] as const;
/**
 * Hold lifecycle on the HITL backend. PENDING = Aegis DECISION_HELD_FOR_HUMAN awaiting operators.
 * RELEASE_DENIED = operators approved but Aegis re-ran its hard controls at release and rejected.
 */
export const HITL_STATUSES = ["PENDING", "APPROVED", "REJECTED", "EXPIRED", "RELEASE_DENIED"] as const;
export const DECISIONS = ["APPROVE", "REJECT"] as const;

export const MIN_REASON_LENGTH = 10;
export const MAX_REASON_LENGTH = 1000;

export type Side = (typeof SIDES)[number];
export type Regime = (typeof REGIMES)[number];
export type HitlStatus = (typeof HITL_STATUSES)[number];
export type Decision = (typeof DECISIONS)[number];

/** int64 nanoseconds since epoch, transmitted as a decimal string (exceeds 2^53 as a JSON number). */
const nsSchema = z.string().regex(/^\d{1,19}$/, "expected int64 decimal string");
/** Fixed-point money/quantity/price: int64 in units of 1e-9 ("nanos"), decimal string. Never a float. */
const nanosSchema = z.string().regex(/^-?\d{1,19}$/, "expected int64 nanos decimal string");
const finite = z.number().finite();
const idSchema = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/, "invalid id");
const textSchema = z.string().max(4000);

export const approvalSchema = z.object({
  approverSub: z.string().min(1).max(128),
  decision: z.enum(DECISIONS),
  reason: z.string().max(MAX_REASON_LENGTH),
  decidedAtNs: nsSchema,
});

export const debateSchema = z.object({
  blue: textSchema,
  red: textSchema,
  judge: textSchema,
});

/** One evaluated Aegis control (proto ControlResult). Shows why the signal was held. */
export const controlSchema = z.object({
  controlId: z.string().max(16),
  isHard: z.boolean(),
  passed: z.boolean(),
  reason: z.string().max(64),
  threshold: z.string().max(64),
  observed: z.string().max(64),
  detail: z.string().max(500),
});

/**
 * A held signal: proto TradeSignal fields plus the Aegis hold (AegisDecision with
 * DECISION_HELD_FOR_HUMAN) and the HITL approval state. `holdId` is the resource id.
 */
export const signalSchema = z.object({
  holdId: idSchema,
  signalId: idSchema,
  symbol: z.string().min(1).max(16),
  createdAtNs: nsSchema,
  side: z.enum(SIDES),
  quantityNanos: nanosSchema,
  priceLimitNanos: nanosSchema,
  estimatedSpreadCostNanos: nanosSchema,
  estimatedMarketImpactNanos: nanosSchema,
  estimatedVenueFeesNanos: nanosSchema,
  estimatedTotalCostNanos: nanosSchema,
  omega: finite.min(0).max(1),
  expectedValue: finite,
  pSuccess: finite.min(0).max(1),
  pFailure: finite.min(0).max(1),
  rewardEstimate: finite,
  riskEstimate: finite,
  regime: z.enum(REGIMES),
  regimeConfidence: finite.min(0).max(1),
  strategyId: z.string().max(64).default(""),
  debateSummary: textSchema,
  debate: debateSchema.optional(),
  validUntilNs: nsSchema,
  holdExpiresAtNs: nsSchema,
  heldReasons: z.array(z.string().max(64)).default([]),
  controls: z.array(controlSchema).default([]),
  hitlStatus: z.enum(HITL_STATUSES),
  requiredApprovals: z.number().int().min(1).max(2).default(1),
  approvals: z.array(approvalSchema).default([]),
});

export type Signal = z.infer<typeof signalSchema>;
export type Approval = z.infer<typeof approvalSchema>;

export const holdListSchema = z.object({ holds: z.array(signalSchema) });

/** Browser -> BFF request body for POST /api/signals/{id}/decision. Strict: unknown keys are rejected. */
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/;

export const decisionBodySchema = z
  .object({
    decision: z.enum(["approve", "reject"]),
    reason: z
      .string()
      .transform((value) => value.trim())
      .pipe(
        z
          .string()
          .min(MIN_REASON_LENGTH, `A reason of at least ${MIN_REASON_LENGTH} characters is required`)
          .max(MAX_REASON_LENGTH)
          .refine((value) => !CONTROL_CHARS.test(value), "Reason contains control characters"),
      ),
  })
  .strict();

export type DecisionBody = z.infer<typeof decisionBodySchema>;

export const signalIdParamSchema = idSchema;

/** Response of POST /v1/holds/{holdId}/decisions on the backend: the updated signal. */
export const decisionResponseSchema = signalSchema;

export const backendErrorSchema = z.object({
  error: z.object({ code: z.string().max(64), message: z.string().max(500) }),
});
