import { mintDemoToken, isDemoPersona } from "@/lib/demo";
import { verifyOperatorToken } from "@/lib/auth/verify";
import type { AppConfig } from "@/lib/config";
import { isAllowedOrigin } from "@/lib/security/csrf";
import type { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";
import { clearedSessionCookie, clientKey, sessionCookie } from "./http";

export interface AuthHandlerDeps {
  readonly config: AppConfig;
  readonly limiter: SlidingWindowRateLimiter;
  readonly now?: () => number;
}

const MAX_TOKEN_LENGTH = 4096;

function redirect(request: Request, path: string, cookie?: string): Response {
  const headers = new Headers({ location: new URL(path, request.url).toString(), "cache-control": "no-store" });
  if (cookie) headers.append("set-cookie", cookie);
  return new Response(null, { status: 303, headers });
}

function originOk(request: Request, config: AppConfig): boolean {
  return isAllowedOrigin({
    origin: request.headers.get("origin"),
    host: request.headers.get("host"),
    allowedOrigins: config.allowedOrigins,
  });
}

async function formField(request: Request, name: string): Promise<string | null> {
  try {
    const value = (await request.formData()).get(name);
    return typeof value === "string" ? value : null;
  } catch {
    return null;
  }
}

/** POST /api/auth/login: verify a pasted operator token, then set the HttpOnly session cookie. */
export async function handleLogin(request: Request, deps: AuthHandlerDeps): Promise<Response> {
  const { config } = deps;
  if (!originOk(request, config)) return new Response("Cross-origin request refused", { status: 403 });
  if (!deps.limiter.check(`login:${clientKey(request)}`).allowed) {
    return new Response("Too many attempts", { status: 429, headers: { "retry-after": "60" } });
  }
  const token = (await formField(request, "token"))?.trim();
  if (!token || token.length > MAX_TOKEN_LENGTH) return redirect(request, "/login?error=invalid");

  const result = await verifyOperatorToken(token, config, new Date((deps.now ?? Date.now)()));
  if (!result.ok) return redirect(request, "/login?error=invalid");

  const maxAge = result.session.expiresAt - Math.floor((deps.now ?? Date.now)() / 1000);
  return redirect(request, "/", sessionCookie(token, maxAge, config.isProduction));
}

/** POST /api/auth/demo: DEMO mode only. Signs in as one of the synthetic personas. */
export async function handleDemoLogin(request: Request, deps: AuthHandlerDeps): Promise<Response> {
  const { config } = deps;
  if (!config.demoMode || config.isProduction) return new Response("Not found", { status: 404 });
  if (!originOk(request, config)) return new Response("Cross-origin request refused", { status: 403 });
  if (!deps.limiter.check(`login:${clientKey(request)}`).allowed) {
    return new Response("Too many attempts", { status: 429, headers: { "retry-after": "60" } });
  }
  const persona = await formField(request, "persona");
  if (!persona || !isDemoPersona(persona)) return redirect(request, "/login?error=invalid");

  const nowMs = (deps.now ?? Date.now)();
  const token = await mintDemoToken(config, persona, nowMs);
  return redirect(request, "/", sessionCookie(token, 3600, false));
}

export function handleLogout(request: Request, deps: AuthHandlerDeps): Response {
  if (!originOk(request, deps.config)) return new Response("Cross-origin request refused", { status: 403 });
  return redirect(request, "/login", clearedSessionCookie(deps.config.isProduction));
}
