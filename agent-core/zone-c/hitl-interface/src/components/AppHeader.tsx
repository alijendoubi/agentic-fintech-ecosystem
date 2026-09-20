import Link from "next/link";

interface AppHeaderProps {
  readonly sub: string | null;
  readonly role: string | null;
  readonly demoMode: boolean;
}

export function AppHeader({ sub, role, demoMode }: AppHeaderProps) {
  return (
    <header className="app-header">
      {demoMode ? (
        <p className="demo-banner" role="alert">
          DEMO MODE: synthetic data and personas. Nothing here is a real signal, approval or order.
        </p>
      ) : null}
      <div className="app-header-row">
        <Link href="/" className="brand">
          AFE HITL Terminal
        </Link>
        {sub ? (
          <form action="/api/auth/logout" method="post" className="session">
            <span>
              Signed in as <strong>{sub}</strong> ({role})
            </span>
            <button type="submit" className="btn btn-link">
              Sign out
            </button>
          </form>
        ) : null}
      </div>
    </header>
  );
}
