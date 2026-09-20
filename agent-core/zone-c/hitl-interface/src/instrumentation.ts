/**
 * Runs once when the server starts. If the configuration is unsafe (missing,
 * short or placeholder HITL_JWT_SECRET, missing backend URL, demo mode in
 * production) getConfig() throws and the server refuses to start.
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  const { getConfig } = await import("./lib/config");
  getConfig();
}
