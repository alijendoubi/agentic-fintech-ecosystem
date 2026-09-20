import { evaluateDecision, msToNs } from "@/lib/signals/policy";
import type { FourEyesConfig } from "@/lib/signals/policy";
import type { Signal } from "@/lib/signals/schema";
import { err, ok } from "./types";
import type { ApiError, DecisionSubmission, HitlApiClient, OperatorContext, Result } from "./types";

export interface MockAuditEntry {
  readonly atNs: string;
  readonly holdId: string;
  readonly actorSub: string;
  readonly decision: string;
  readonly reason: string;
  readonly outcome: "recorded" | "denied";
  readonly detail: string;
}

export interface MockClientOptions {
  readonly signals: readonly Signal[];
  readonly fourEyes: FourEyesConfig;
  readonly now?: () => number;
  /** When set, every call fails with this error (used to test deny-on-error). */
  readonly failure?: ApiError;
}

/**
 * In-memory stand-in for the HITL backend. It applies the same rules the real
 * backend must enforce, keeps immutable signal records (each decision replaces
 * the record) and keeps an audit trail of every attempt, including denials.
 * Used by tests and by the DEMO mode; it must never be used in production.
 */
export class MockHitlApiClient implements HitlApiClient {
  private signals: ReadonlyMap<string, Signal>;
  private audit: readonly MockAuditEntry[] = [];
  private readonly seenRequests = new Map<string, Signal>();
  private readonly fourEyes: FourEyesConfig;
  private readonly now: () => number;
  private failure: ApiError | undefined;

  constructor(options: MockClientOptions) {
    this.signals = new Map(options.signals.map((signal) => [signal.holdId, signal]));
    this.fourEyes = options.fourEyes;
    this.now = options.now ?? Date.now;
    this.failure = options.failure;
  }

  setFailure(failure: ApiError | undefined): void {
    this.failure = failure;
  }

  auditLog(): readonly MockAuditEntry[] {
    return this.audit;
  }

  async listHolds(_ctx: OperatorContext): Promise<Result<readonly Signal[]>> {
    if (this.failure) return { ok: false, error: this.failure };
    return ok([...this.signals.values()]);
  }

  async getHold(holdId: string, _ctx: OperatorContext): Promise<Result<Signal>> {
    if (this.failure) return { ok: false, error: this.failure };
    const signal = this.signals.get(holdId);
    return signal ? ok(signal) : err("not_found", "Hold not found");
  }

  async submitDecision(submission: DecisionSubmission, _ctx: OperatorContext): Promise<Result<Signal>> {
    if (this.failure) return { ok: false, error: this.failure };
    const replay = this.seenRequests.get(submission.clientRequestId);
    if (replay) return ok(replay);

    const current = this.signals.get(submission.holdId);
    if (!current) return err("not_found", "Hold not found");

    const nowNs = msToNs(this.now());
    const verdict = evaluateDecision({
      signal: current,
      actor: submission.approver,
      decision: submission.decision,
      reason: submission.reason,
      nowNs,
      fourEyes: this.fourEyes,
    });
    if (!verdict.allowed) {
      this.record(submission, nowNs, "denied", verdict.code);
      return err("refused", verdict.message);
    }

    const updated = applyDecision(current, submission, verdict.finalizes, verdict.required, nowNs);
    this.signals = new Map(this.signals).set(updated.holdId, updated);
    this.seenRequests.set(submission.clientRequestId, updated);
    this.record(submission, nowNs, "recorded", verdict.finalizes ? "finalized" : "awaiting_second_approver");
    return ok(updated);
  }

  private record(
    submission: DecisionSubmission,
    nowNs: bigint,
    outcome: MockAuditEntry["outcome"],
    detail: string,
  ): void {
    const entry: MockAuditEntry = {
      atNs: nowNs.toString(),
      holdId: submission.holdId,
      actorSub: submission.approver.sub,
      decision: submission.decision,
      reason: submission.reason,
      outcome,
      detail,
    };
    this.audit = [...this.audit, entry];
  }
}

function applyDecision(
  signal: Signal,
  submission: DecisionSubmission,
  finalizes: boolean,
  required: number,
  nowNs: bigint,
): Signal {
  const approval = {
    approverSub: submission.approver.sub,
    decision: submission.decision,
    reason: submission.reason,
    decidedAtNs: nowNs.toString(),
  };
  const hitlStatus = submission.decision === "REJECT" ? "REJECTED" : finalizes ? "APPROVED" : "PENDING";
  return {
    ...signal,
    requiredApprovals: required === 2 ? 2 : 1,
    approvals: [...signal.approvals, approval],
    hitlStatus,
  };
}
