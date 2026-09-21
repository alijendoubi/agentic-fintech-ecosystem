import type { Role } from "@/lib/auth/verify";
import type { Decision, Signal } from "@/lib/signals/schema";

/**
 * Typed boundary to the HITL backend. Implementations: HttpHitlApiClient
 * (real) and MockHitlApiClient (tests and clearly-labelled local demo mode).
 * The contract is documented in docs/api-contract.md.
 */

export type ApiErrorCode =
  | "timeout"
  | "network"
  | "unauthorized"
  | "forbidden"
  | "not_found"
  | "refused"
  | "unavailable"
  | "server_error"
  | "invalid_response";

export interface ApiError {
  readonly code: ApiErrorCode;
  readonly message: string;
}

export type Result<T> =
  | { readonly ok: true; readonly value: T }
  | { readonly ok: false; readonly error: ApiError };

export function ok<T>(value: T): Result<T> {
  return { ok: true, value };
}

export function err<T = never>(code: ApiErrorCode, message: string): Result<T> {
  return { ok: false, error: { code, message } };
}

/** The verified operator on whose behalf a call is made. `token` is forwarded so the backend can re-verify. */
export interface OperatorContext {
  readonly token: string;
  readonly sub: string;
}

export interface ApproverIdentity {
  readonly sub: string;
  readonly role: Role;
  readonly amr: readonly string[];
}

export interface DecisionSubmission {
  readonly holdId: string;
  readonly decision: Decision;
  readonly reason: string;
  readonly approver: ApproverIdentity;
  /** Idempotency key: a retried POST must not record a second decision. */
  readonly clientRequestId: string;
}

export interface HitlApiClient {
  listHolds(ctx: OperatorContext): Promise<Result<readonly Signal[]>>;
  getHold(holdId: string, ctx: OperatorContext): Promise<Result<Signal>>;
  submitDecision(submission: DecisionSubmission, ctx: OperatorContext): Promise<Result<Signal>>;
}
