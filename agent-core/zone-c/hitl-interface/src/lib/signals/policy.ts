import { canAct } from "@/lib/auth/verify";
import type { OperatorSession } from "@/lib/auth/verify";
import type { AppConfig } from "@/lib/config";
import { MIN_REASON_LENGTH } from "./schema";
import type { Decision, Signal } from "./schema";

/**
 * Pure decision rules. The backend (and Aegis behind it) is authoritative; this
 * module is used by the BFF as a pre-flight (defence in depth, fast feedback)
 * and by the mock backend so that demo/test behaviour matches the contract.
 */

export type DisplayStatus =
  | "PENDING"
  | "AWAITING_SECOND_APPROVER"
  | "APPROVED"
  | "REJECTED"
  | "RELEASE_DENIED"
  | "EXPIRED";

export type PolicyCode = "forbidden" | "reason_required" | "expired" | "already_final" | "duplicate_approver";

export type PolicyVerdict =
  | { readonly allowed: true; readonly required: number; readonly finalizes: boolean }
  | { readonly allowed: false; readonly code: PolicyCode; readonly message: string };

export type FourEyesConfig = AppConfig["fourEyes"];
export type PolicyActor = Pick<OperatorSession, "sub" | "role" | "amr">;

const NS_PER_MS = 1_000_000n;
const NANOS_PER_UNIT = 1_000_000_000n;

export function msToNs(ms: number): bigint {
  return BigInt(Math.trunc(ms)) * NS_PER_MS;
}

/** Converts a configured whole/decimal number to int64 nanos so comparisons are done in integers. */
export function toNanos(value: number): bigint {
  return BigInt(Math.round(value * Number(NANOS_PER_UNIT)));
}

/** Expired once either the signal validity or the Aegis hold window has elapsed (exclusive boundary). */
export function isExpired(signal: Pick<Signal, "validUntilNs" | "holdExpiresAtNs">, nowNs: bigint): boolean {
  return nowNs >= BigInt(signal.validUntilNs) || nowNs >= BigInt(signal.holdExpiresAtNs);
}

/** Number of distinct approvers required: the stricter of the backend's value and the local size threshold. */
export function requiredApprovals(
  signal: Pick<Signal, "quantityNanos" | "priceLimitNanos" | "requiredApprovals">,
  fourEyes: FourEyesConfig,
): number {
  const quantity = BigInt(signal.quantityNanos);
  const priceLimit = BigInt(signal.priceLimitNanos);
  const overQuantity = quantity >= toNanos(fourEyes.quantityThreshold);
  const notionalNanos = (quantity * priceLimit) / NANOS_PER_UNIT;
  const overNotional =
    fourEyes.notionalThresholdUsd !== null &&
    priceLimit > 0n &&
    notionalNanos >= toNanos(fourEyes.notionalThresholdUsd);
  const byThreshold = overQuantity || overNotional ? 2 : 1;
  return Math.max(signal.requiredApprovals, byThreshold);
}

export function deriveStatus(signal: Signal, nowNs: bigint): DisplayStatus {
  if (signal.hitlStatus !== "PENDING") {
    return signal.hitlStatus;
  }
  if (isExpired(signal, nowNs)) return "EXPIRED";
  const approvals = signal.approvals.filter((a) => a.decision === "APPROVE").length;
  return approvals > 0 ? "AWAITING_SECOND_APPROVER" : "PENDING";
}

function deny(code: PolicyCode, message: string): PolicyVerdict {
  return { allowed: false, code, message };
}

export interface DecisionInput {
  readonly signal: Signal;
  readonly actor: PolicyActor;
  readonly decision: Decision;
  readonly reason: string;
  readonly nowNs: bigint;
  readonly fourEyes: FourEyesConfig;
}

export function evaluateDecision(input: DecisionInput): PolicyVerdict {
  const { signal, actor, decision, reason, nowNs, fourEyes } = input;

  if (!canAct(actor)) {
    return deny("forbidden", "Only approvers with MFA evidence can approve or reject signals.");
  }
  if (reason.trim().length < MIN_REASON_LENGTH) {
    return deny("reason_required", `A reason of at least ${MIN_REASON_LENGTH} characters is mandatory.`);
  }
  const status = deriveStatus(signal, nowNs);
  if (status === "EXPIRED") {
    return deny("expired", "This signal has expired and can no longer be approved or rejected.");
  }
  if (status !== "PENDING" && status !== "AWAITING_SECOND_APPROVER") {
    const label = status.toLowerCase().replace("_", " ");
    return deny("already_final", `This signal is already ${label}; the decision is final.`);
  }

  const required = requiredApprovals(signal, fourEyes);
  if (decision === "REJECT") {
    return { allowed: true, required, finalizes: true };
  }

  const alreadyApproved = signal.approvals.some((a) => a.decision === "APPROVE" && a.approverSub === actor.sub);
  if (alreadyApproved) {
    return deny("duplicate_approver", "You have already approved this signal; a different approver is required.");
  }
  const approvalsAfter = signal.approvals.filter((a) => a.decision === "APPROVE").length + 1;
  return { allowed: true, required, finalizes: approvalsAfter >= required };
}
