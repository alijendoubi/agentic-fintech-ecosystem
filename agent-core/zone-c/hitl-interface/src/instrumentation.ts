/**
 * Runs once when the server starts. If the configuration is unsafe (missing,
 * short or placeholder HITL_JWT_SECRET, missing backend URL, demo mode in
 * production) the process exits with code 1 (see lib/startup-guard.ts).
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { enforceStartupConfig } = await import("./lib/startup-guard");
  enforceStartupConfig(process.env);
}
