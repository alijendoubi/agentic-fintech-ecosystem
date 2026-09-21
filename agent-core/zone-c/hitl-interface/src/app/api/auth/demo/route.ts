import { handleDemoLogin } from "@/server/auth-handlers";
import { getRuntime } from "@/server/runtime";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  try {
    const { config, loginLimiter } = getRuntime();
    return await handleDemoLogin(request, { config, limiter: loginLimiter });
  } catch {
    return new Response("Service unavailable", { status: 503 });
  }
}
