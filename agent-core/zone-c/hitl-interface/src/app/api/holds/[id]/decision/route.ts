import { handleDecisionRequest } from "@/server/decision-handler";
import { jsonResponse } from "@/server/http";
import { getRuntime } from "@/server/runtime";

export const dynamic = "force-dynamic";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }): Promise<Response> {
  try {
    const { config, client, mutationLimiter } = getRuntime();
    const { id } = await context.params;
    return await handleDecisionRequest(request, id, { config, client, limiter: mutationLimiter });
  } catch {
    // Any unexpected failure (including invalid configuration) is a denial.
    return jsonResponse({ ok: false, code: "internal_error", message: "Not approved. Internal error.", denied: true }, 500);
  }
}
