/**
 * Persistent transparency label shown on every AI-generated signal (intent:
 * EU AI Act Art. 50 transparency, see docs/regulatory/eu-ai-act-limited-risk-disclosure.md).
 * This label is a UI disclosure only; it is not a legal conclusion.
 */
export function AiLabel() {
  return (
    <p className="ai-label" role="note">
      <strong>AI-generated signal</strong>
      <span> produced by the Cognitive Core (Blue/Red/Judge debate). A human decision is required before it can proceed.</span>
    </p>
  );
}
