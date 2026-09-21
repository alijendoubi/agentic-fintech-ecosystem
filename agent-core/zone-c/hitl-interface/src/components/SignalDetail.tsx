import { formatDecimal, formatNanos, formatProbability, formatUtc, humanizeEnum, nsToMs } from "@/lib/format";
import type { DisplayStatus } from "@/lib/signals/policy";
import type { Signal } from "@/lib/signals/schema";
import { AiLabel } from "./AiLabel";
import { ExpiryCountdown } from "./ExpiryCountdown";
import { StatusBadge } from "./StatusBadge";

interface SignalDetailProps {
  readonly signal: Signal;
  readonly status: DisplayStatus;
  readonly required: number;
  readonly serverNowMs: number;
}

function Row({ label, children }: { readonly label: string; readonly children: React.ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </>
  );
}

export function SignalDetail({ signal, status, required, serverNowMs }: SignalDetailProps) {
  const expiresAtMs = Math.min(nsToMs(signal.validUntilNs), nsToMs(signal.holdExpiresAtNs));
  return (
    <article aria-labelledby="signal-title">
      <AiLabel />
      <h1 id="signal-title">
        {signal.side.replace("_", " ")} {formatNanos(signal.quantityNanos, 4)} {signal.symbol}
      </h1>
      <p>
        <StatusBadge status={status} /> &middot; Expires in{" "}
        <ExpiryCountdown expiresAtMs={expiresAtMs} serverNowMs={serverNowMs} /> ({formatUtc(expiresAtMs)})
      </p>

      <h2>Signal</h2>
      <dl className="grid">
        <Row label="Signal id">{signal.signalId}</Row>
        <Row label="Hold id">{signal.holdId}</Row>
        <Row label="Strategy">{signal.strategyId || "n/a"}</Row>
        <Row label="Limit price">{signal.priceLimitNanos === "0" ? "Market order" : formatNanos(signal.priceLimitNanos, 4)}</Row>
        <Row label="Omega (judge confidence)">{formatDecimal(signal.omega, 3)}</Row>
        <Row label="Regime">
          {humanizeEnum(signal.regime)} (confidence {formatProbability(signal.regimeConfidence)})
        </Row>
        <Row label="Required approvals">{required}</Row>
      </dl>

      <h2>Expected value</h2>
      <dl className="grid">
        <Row label="Expected value">{formatDecimal(signal.expectedValue)}</Row>
        <Row label="P(success) / P(failure)">
          {formatProbability(signal.pSuccess)} / {formatProbability(signal.pFailure)}
        </Row>
        <Row label="Reward / risk estimate">
          {formatDecimal(signal.rewardEstimate)} / {formatDecimal(signal.riskEstimate)}
        </Row>
        <Row label="Est. spread cost">{formatNanos(signal.estimatedSpreadCostNanos)}</Row>
        <Row label="Est. market impact">{formatNanos(signal.estimatedMarketImpactNanos)}</Row>
        <Row label="Est. venue fees">{formatNanos(signal.estimatedVenueFeesNanos)}</Row>
        <Row label="Est. total cost">{formatNanos(signal.estimatedTotalCostNanos)}</Row>
      </dl>

      <h2>Blue / Red / Judge debate</h2>
      <p className="debate-summary">{signal.debateSummary}</p>
      {signal.debate ? (
        <div className="debate">
          <section aria-labelledby="debate-blue"><h3 id="debate-blue">Blue (proposer)</h3><p>{signal.debate.blue}</p></section>
          <section aria-labelledby="debate-red"><h3 id="debate-red">Red (challenger)</h3><p>{signal.debate.red}</p></section>
          <section aria-labelledby="debate-judge"><h3 id="debate-judge">Judge (synthesis)</h3><p>{signal.debate.judge}</p></section>
        </div>
      ) : null}

      <h2>Why this signal is held for a human</h2>
      {signal.controls.length === 0 ? (
        <p>{signal.heldReasons.map(humanizeEnum).join(", ") || "No control detail supplied by the backend."}</p>
      ) : (
        <table>
          <caption className="visually-hidden">Aegis controls evaluated for this signal</caption>
          <thead>
            <tr><th scope="col">Control</th><th scope="col">Kind</th><th scope="col">Result</th><th scope="col">Reason</th><th scope="col">Threshold / observed</th></tr>
          </thead>
          <tbody>
            {signal.controls.map((control) => (
              <tr key={control.controlId}>
                <th scope="row">{control.controlId}</th>
                <td>{control.isHard ? "Hard" : "Soft"}</td>
                <td>{control.passed ? "Passed" : "Failed"}</td>
                <td>{control.passed ? "" : humanizeEnum(control.reason)}</td>
                <td>{control.threshold} / {control.observed}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Decision history</h2>
      {signal.approvals.length === 0 ? (
        <p>No decisions recorded yet.</p>
      ) : (
        <ul>
          {signal.approvals.map((approval) => (
            <li key={`${approval.approverSub}-${approval.decidedAtNs}`}>
              {approval.decision === "APPROVE" ? "Approved" : "Rejected"} by {approval.approverSub} at{" "}
              {formatUtc(nsToMs(approval.decidedAtNs))}: {approval.reason}
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}
