"use client";

import { useRouter } from "next/navigation";
import { useId, useState } from "react";
import type { FormEvent } from "react";
import { MIN_REASON_LENGTH } from "@/lib/signals/schema";
import { useServerClock } from "./useServerClock";

interface DecisionFormProps {
  readonly holdId: string;
  readonly csrfToken: string;
  /** Server-computed: null when this operator may act, otherwise the reason they may not. */
  readonly blockedReason: string | null;
  readonly expiresAtMs: number;
  readonly serverNowMs: number;
  readonly requiredApprovals: number;
  readonly approvalsSoFar: number;
}

type Notice = { readonly kind: "success" | "denied"; readonly message: string };
type Choice = "approve" | "reject";

const REQUEST_TIMEOUT_MS = 10_000;
const NO_CONFIRMATION =
  "No confirmation was received from the backend. Treat this as NOT approved and reload to verify the current state.";

function extractMessage(payload: unknown): string | null {
  if (typeof payload === "object" && payload !== null && "message" in payload) {
    const message = (payload as { message: unknown }).message;
    return typeof message === "string" ? message : null;
  }
  return null;
}

function isConfirmed(payload: unknown): boolean {
  return typeof payload === "object" && payload !== null && (payload as { ok?: unknown }).ok === true;
}

async function postDecision(holdId: string, csrfToken: string, decision: Choice, reason: string): Promise<Notice> {
  try {
    const response = await fetch(`/api/holds/${encodeURIComponent(holdId)}/decision`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json", "x-csrf-token": csrfToken },
      body: JSON.stringify({ decision, reason }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    const payload: unknown = await response.json().catch(() => null);
    if (response.ok && isConfirmed(payload)) {
      return { kind: "success", message: decision === "approve" ? "Approval recorded." : "Rejection recorded." };
    }
    return { kind: "denied", message: extractMessage(payload) ?? NO_CONFIRMATION };
  } catch {
    return { kind: "denied", message: NO_CONFIRMATION };
  }
}

export function DecisionForm(props: DecisionFormProps) {
  const router = useRouter();
  const reasonId = useId();
  const hintId = useId();
  const now = useServerClock(props.serverNowMs);
  const [reason, setReason] = useState("");
  const [pending, setPending] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const expired = props.expiresAtMs - now <= 0;
  const blocked = props.blockedReason ?? (expired ? "This signal has expired and cannot be approved or rejected." : null);
  const reasonTooShort = reason.trim().length < MIN_REASON_LENGTH;

  async function submit(decision: Choice): Promise<void> {
    if (blocked || pending) return;
    if (reasonTooShort) {
      setNotice({ kind: "denied", message: `A reason of at least ${MIN_REASON_LENGTH} characters is mandatory.` });
      return;
    }
    setPending(true);
    setNotice(null);
    const result = await postDecision(props.holdId, props.csrfToken, decision, reason.trim());
    setNotice(result);
    setPending(false);
    if (result.kind === "success") {
      setReason("");
      router.refresh();
    }
  }

  function onSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
  }

  return (
    <form className="decision-form" onSubmit={onSubmit} aria-labelledby={`${reasonId}-title`}>
      <h2 id={`${reasonId}-title`}>Operator decision</h2>
      <p>
        Approvals recorded: {props.approvalsSoFar} of {props.requiredApprovals} required
        {props.requiredApprovals > 1 ? " (four-eyes: two different approvers)" : ""}. Reject is final.
      </p>
      {blocked ? (
        <p className="notice notice-denied" role="alert">
          {blocked}
        </p>
      ) : null}
      <label htmlFor={reasonId}>Reason (mandatory, free text)</label>
      <textarea
        id={reasonId}
        name="reason"
        rows={4}
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        disabled={blocked !== null || pending}
        aria-describedby={hintId}
        required
        maxLength={1000}
      />
      <p id={hintId} className="hint">
        At least {MIN_REASON_LENGTH} characters. The reason is stored in the audit log with your identity.
      </p>
      <div className="actions">
        <button type="button" className="btn btn-approve" disabled={blocked !== null || pending} onClick={() => void submit("approve")}>
          Approve
        </button>
        <button type="button" className="btn btn-reject" disabled={blocked !== null || pending} onClick={() => void submit("reject")}>
          Reject
        </button>
      </div>
      <div aria-live="polite">
        {notice ? (
          <p className={`notice notice-${notice.kind}`} role={notice.kind === "denied" ? "alert" : "status"}>
            {notice.kind === "denied" ? "Not approved. " : ""}
            {notice.message}
          </p>
        ) : null}
      </div>
    </form>
  );
}
