import { getConfig } from "@/lib/config";

export const dynamic = "force-dynamic";

/** Liveness/readiness for container healthchecks. Unauthenticated; exposes no data beyond ok/unhealthy. */
export function GET(): Response {
  try {
    getConfig();
    return Response.json({ status: "ok" });
  } catch {
    return Response.json({ status: "unhealthy" }, { status: 503 });
  }
}
