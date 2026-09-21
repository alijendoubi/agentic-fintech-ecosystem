import { randomUUID } from "node:crypto";
import type { ApiError, HitlApiClient } from "@/lib/api/types";
import { canAct } from "@/lib/auth/verify";
import type { OperatorSession } from "@/lib/auth/verify";
import type { AppConfig } from "@/lib/config";
import { evaluateDecision, msToNs } from "@/lib/signals/policy";
import type { PolicyCode } from "@/lib/signals/policy";
import type { DecisionBody, Signal } from "@/lib/signals/schema";

export type DecisionFailureCode =
  | PolicyCode
  | "not_found"
  | "unauthorized"
  | "backend_error"
  | "timeout"
  | "invalid_response";

export type DecisionOutcome =
  | { readonly ok: true; readonly signal: Signal }
  | {
      readonly ok: false;
      readonly code: DecisionFailureCode;
      readonly message: string;
      /** Always true for a failure: nothing was approved. */
      readonly denied: true;
    };

export interface DecisionDeps {
  readonly client: HitlApiClient;
  readonly fourEyes: AppConfig["fourEyes"];
  readonly now?: () => number;
  readonly newRequestId?: () => string;
}

const NOT_APPROVED_HINT = "Nothing was approved. Reload the signal to verify its current state.";

function denied(code: DecisionFailureCode, message: string): DecisionOutcome {
  return { ok: false, code, message, denied: true };
}

function fromApiError(error: ApiError): DecisionOutcome {
  switch (error.code) {
    case "not_found":
      return denied("not_found", "Signal not found.");
    case "unauthorized":
    case "forbidden":
      return denied("unauthorized", `The backend refused this operator. ${NOT_APPROVED_HINT}`);
    case "refused":
      return denied("backend_error", `${error.message} ${NOT_APPROVED_HINT}`);
    case "timeout":
      return denied("timeout", `The backend did not respond in time. ${NOT_APPROVED_HINT}`);
    case "invalid_response":
      return denied("invalid_response", `The backend returned an unexpected response. ${NOT_APPROVED_HINT}`);
    default:
      return denied("backend_error", `The backend is unavailable. ${NOT_APPROVED_HINT}`);
  }
}

/**
 * Relays an operator decision to the backend. The UI never decides anything:
 * this function pre-checks (defence in depth), forwards, and treats every error,
 * timeout or unexpected answer as DENY. The backend is the system of record and
 * audit-logs the action.
 */
export async function submitOperatorDecision(
  deps: DecisionDeps,
  session: OperatorSession,
  holdId: string,
  body: DecisionBody,
): Promise<DecisionOutcome> {
  if (!canAct(session)) {
    return denied("forbidden", "Only approvers with MFA evidence can approve or reject signals.");
  }
  const ctx = { token: session.token, sub: session.sub };
  const decision = body.decision === "approve" ? "APPROVE" : "REJECT";
  const now = deps.now ?? Date.now;

  const current = await deps.client.getHold(holdId, ctx);
  if (!current.ok) return fromApiError(current.error);

  const verdict = evaluateDecision({
    signal: current.value,
    actor: session,
    decision,
    reason: body.reason,
    nowNs: msToNs(now()),
    fourEyes: deps.fourEyes,
  });
  if (!verdict.allowed) return denied(verdict.code, verdict.message);

  const submitted = await deps.client.submitDecision(
    {
      holdId,
      decision,
      reason: body.reason,
      approver: { sub: session.sub, role: session.role, amr: session.amr },
      clientRequestId: (deps.newRequestId ?? randomUUID)(),
    },
    ctx,
  );
  if (!submitted.ok) return fromApiError(submitted.error);

  const confirmed =
    submitted.value.holdId === holdId &&
    submitted.value.approvals.some((a) => a.approverSub === session.sub && a.decision === decision);
  if (!confirmed) {
    return denied("invalid_response", `The backend did not confirm the decision. ${NOT_APPROVED_HINT}`);
  }
  return { ok: true, signal: submitted.value };
}
