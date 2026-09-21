import Link from "next/link";
import { notFound } from "next/navigation";
import { AppHeader } from "@/components/AppHeader";
import { DecisionForm } from "@/components/DecisionForm";
import { SignalDetail } from "@/components/SignalDetail";
import { canAct } from "@/lib/auth/verify";
import type { OperatorSession } from "@/lib/auth/verify";
import { nsToMs } from "@/lib/format";
import { deriveStatus, msToNs, requiredApprovals } from "@/lib/signals/policy";
import type { DisplayStatus } from "@/lib/signals/policy";
import { signalIdParamSchema } from "@/lib/signals/schema";
import { deriveCsrfToken } from "@/lib/security/csrf";
import { serverNowMs } from "@/server/clock";
import { getRuntime } from "@/server/runtime";
import { requireSession } from "@/server/session";

export const dynamic = "force-dynamic";

function blockedReason(session: OperatorSession, status: DisplayStatus): string | null {
  if (!canAct(session)) return "Your role or MFA evidence does not allow approving or rejecting signals (view only).";
  if (status !== "PENDING" && status !== "AWAITING_SECOND_APPROVER") {
    return `This signal is ${status.toLowerCase().replace(/_/g, " ")}; no further decision can be recorded.`;
  }
  return null;
}

export default async function SignalPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const parsedId = signalIdParamSchema.safeParse(id);
  if (!parsedId.success) notFound();

  const session = await requireSession();
  const { config, client } = getRuntime();
  const result = await client.getHold(parsedId.data, { token: session.token, sub: session.sub });
  const nowMs = serverNowMs();

  if (!result.ok) {
    if (result.error.code === "not_found") notFound();
    return (
      <>
        <AppHeader sub={session.sub} role={session.role} demoMode={config.demoMode} />
        <main id="main">
          <p className="notice notice-denied" role="alert">
            This signal could not be loaded ({result.error.code}). Nothing can be approved until it loads.
          </p>
          <Link href="/">Back to queue</Link>
        </main>
      </>
    );
  }

  const signal = result.value;
  const status = deriveStatus(signal, msToNs(nowMs));
  const required = requiredApprovals(signal, config.fourEyes);
  const expiresAtMs = Math.min(nsToMs(signal.validUntilNs), nsToMs(signal.holdExpiresAtNs));

  return (
    <>
      <AppHeader sub={session.sub} role={session.role} demoMode={config.demoMode} />
      <main id="main">
        <p>
          <Link href="/">Back to queue</Link>
        </p>
        <SignalDetail signal={signal} status={status} required={required} serverNowMs={nowMs} />
        <DecisionForm
          holdId={signal.holdId}
          csrfToken={deriveCsrfToken(config.jwtSecret, session.token)}
          blockedReason={blockedReason(session, status)}
          expiresAtMs={expiresAtMs}
          serverNowMs={nowMs}
          requiredApprovals={required}
          approvalsSoFar={signal.approvals.filter((a) => a.decision === "APPROVE").length}
        />
      </main>
    </>
  );
}
