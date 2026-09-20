import Link from "next/link";
import { AppHeader } from "@/components/AppHeader";
import { ExpiryCountdown } from "@/components/ExpiryCountdown";
import { StatusBadge } from "@/components/StatusBadge";
import { formatNanos, humanizeEnum, nsToMs } from "@/lib/format";
import { deriveStatus, msToNs } from "@/lib/signals/policy";
import type { Signal } from "@/lib/signals/schema";
import { getRuntime } from "@/server/runtime";
import { requireSession } from "@/server/session";

export const dynamic = "force-dynamic";

function expiryMs(signal: Signal): number {
  return Math.min(nsToMs(signal.validUntilNs), nsToMs(signal.holdExpiresAtNs));
}

export default async function QueuePage() {
  const session = await requireSession();
  const { config, client } = getRuntime();
  const result = await client.listHolds({ token: session.token, sub: session.sub });
  const nowMs = Date.now();
  const nowNs = msToNs(nowMs);
  const signals = result.ok ? [...result.value].sort((a, b) => expiryMs(a) - expiryMs(b)) : [];

  return (
    <>
      <AppHeader sub={session.sub} role={session.role} demoMode={config.demoMode} />
      <main id="main">
        <h1>Pending signals</h1>
        <p className="ai-label" role="note">
          <strong>AI-generated signals.</strong> Each one needs a human decision before it can proceed. Nothing is
          approved automatically.
        </p>
        {!result.ok ? (
          <p className="notice notice-denied" role="alert">
            The signal queue could not be loaded ({result.error.code}). No signal can be actioned while the backend is
            unavailable.
          </p>
        ) : signals.length === 0 ? (
          <p>No signals are waiting for a decision.</p>
        ) : (
          <table>
            <caption className="visually-hidden">Signals awaiting a human decision, soonest expiry first</caption>
            <thead>
              <tr>
                <th scope="col">Symbol</th>
                <th scope="col">Side</th>
                <th scope="col">Quantity</th>
                <th scope="col">Omega</th>
                <th scope="col">Regime</th>
                <th scope="col">Status</th>
                <th scope="col">Expires in</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((signal) => (
                <tr key={signal.holdId}>
                  <th scope="row">
                    <Link href={`/signals/${encodeURIComponent(signal.holdId)}`}>{signal.symbol}</Link>
                  </th>
                  <td>{signal.side.replace("_", " ")}</td>
                  <td>{formatNanos(signal.quantityNanos, 4)}</td>
                  <td>{signal.omega.toFixed(2)}</td>
                  <td>{humanizeEnum(signal.regime)}</td>
                  <td>
                    <StatusBadge status={deriveStatus(signal, nowNs)} />
                  </td>
                  <td>
                    <ExpiryCountdown expiresAtMs={expiryMs(signal)} serverNowMs={nowMs} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </main>
    </>
  );
}
