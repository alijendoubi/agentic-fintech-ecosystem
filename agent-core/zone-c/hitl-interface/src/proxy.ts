import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";
import { buildCsp, generateNonce, staticSecurityHeaders } from "@/lib/security/headers";

/**
 * Sets a per-request CSP nonce and the static security headers on every
 * response. Authentication is NOT decided here: pages and route handlers each
 * verify the operator themselves (this file is defence in depth for headers only).
 */
export function proxy(request: NextRequest): NextResponse {
  const isProduction = process.env.NODE_ENV === "production";
  const nonce = generateNonce();
  const csp = buildCsp({ nonce, isDevelopment: process.env.NODE_ENV === "development" });

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  for (const [name, value] of Object.entries(staticSecurityHeaders(isProduction))) {
    response.headers.set(name, value);
  }
  return response;
}

export const config = {
  matcher: [{ source: "/((?!_next/static|_next/image|favicon.ico).*)" }],
};
