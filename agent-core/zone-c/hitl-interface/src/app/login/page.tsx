import { AppHeader } from "@/components/AppHeader";
import { DEMO_PERSONAS } from "@/lib/demo";
import { getRuntime } from "@/server/runtime";

export const dynamic = "force-dynamic";

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ error?: string }> }) {
  const { config } = getRuntime();
  const { error } = await searchParams;

  return (
    <>
      <AppHeader sub={null} role={null} demoMode={config.demoMode} />
      <main id="main">
        <h1>Sign in</h1>
        {error ? (
          <p className="notice notice-denied" role="alert">
            Sign-in failed. The token is missing, expired, incorrectly signed, or lacks the required role or MFA evidence.
          </p>
        ) : null}

        {config.demoMode ? (
          <form action="/api/auth/demo" method="post">
            <fieldset>
              <legend>DEMO personas (synthetic, not real people)</legend>
              {DEMO_PERSONAS.map((persona, index) => (
                <p key={persona.id}>
                  <label>
                    <input type="radio" name="persona" value={persona.id} defaultChecked={index === 0} /> {persona.id} ({persona.role})
                  </label>
                </p>
              ))}
            </fieldset>
            <button type="submit" className="btn">
              Sign in to DEMO
            </button>
          </form>
        ) : (
          <form action="/api/auth/login" method="post">
            <p>
              Paste the operator token issued for you. It must carry your subject, an approver or viewer role, and MFA
              evidence (amr contains &quot;mfa&quot;).
            </p>
            <label htmlFor="token">Operator token (JWT)</label>
            <textarea id="token" name="token" rows={5} required autoComplete="off" spellCheck={false} />
            <button type="submit" className="btn">
              Sign in
            </button>
            <p className="hint">
              TODO(owner): SSO / IdP (OIDC) sign-in is not implemented. This token form is a stopgap; a fronting gateway
              may instead inject an Authorization: Bearer header.
            </p>
          </form>
        )}
      </main>
    </>
  );
}
