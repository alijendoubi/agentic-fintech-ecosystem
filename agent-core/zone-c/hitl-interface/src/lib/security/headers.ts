/**
 * Security headers. The CSP is per-request (nonce) and set in proxy.ts; the
 * static set is applied there too so every response carries both.
 *
 * style-src keeps 'unsafe-inline' because Next.js/React emit inline style
 * attributes; scripts are nonce-only ('strict-dynamic'), no 'unsafe-inline'.
 */

export interface CspOptions {
  readonly nonce: string;
  readonly isDevelopment: boolean;
}

export function buildCsp({ nonce, isDevelopment }: CspOptions): string {
  const scriptSrc = ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(isDevelopment ? ["'unsafe-eval'"] : [])];
  const directives = [
    "default-src 'self'",
    `script-src ${scriptSrc.join(" ")}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    ...(isDevelopment ? [] : ["upgrade-insecure-requests"]),
  ];
  return directives.join("; ");
}

export function staticSecurityHeaders(isProduction: boolean): Readonly<Record<string, string>> {
  return {
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cache-Control": "no-store",
    ...(isProduction ? { "Strict-Transport-Security": "max-age=63072000; includeSubDomains" } : {}),
  };
}

export function generateNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}
