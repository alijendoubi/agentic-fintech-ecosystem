/**
 * Where does the operator token come from? This is the identity adapter
 * boundary: a fronting OIDC-aware gateway (or oauth2-proxy style sidecar) can
 * inject `Authorization: Bearer <jwt>`; otherwise the token comes from the
 * HttpOnly session cookie set by /api/auth/login.
 *
 * TODO(owner): SSO / IdP integration (OIDC authorization-code + PKCE, JWKS
 * verification) is NOT implemented. Choose the IdP, then add a TokenVerifier
 * (see ./verify.ts) and replace the paste-a-token login form.
 */

export const SESSION_COOKIE = "hitl_session";

export interface TokenSources {
  readonly authorization: string | null | undefined;
  readonly cookie: string | null | undefined;
}

const BEARER_PREFIX = /^Bearer\s+(\S+)$/i;

export function extractToken(sources: TokenSources): string | null {
  const header = sources.authorization?.trim();
  if (header) {
    const match = BEARER_PREFIX.exec(header);
    if (match?.[1]) return match[1];
  }
  const cookie = sources.cookie?.trim();
  return cookie ? cookie : null;
}
