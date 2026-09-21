import { errors, jwtVerify } from "jose";
import { z } from "zod";
import type { AppConfig } from "@/lib/config";

export const ROLES = ["approver", "viewer"] as const;
export type Role = (typeof ROLES)[number];

/** A verified operator. `token` is the raw JWT; it stays server-side (forwarded to the backend, never rendered). */
export interface OperatorSession {
  readonly sub: string;
  readonly role: Role;
  readonly amr: readonly string[];
  readonly expiresAt: number;
  readonly token: string;
}

export type AuthFailureCode =
  | "missing_token"
  | "invalid_token"
  | "expired"
  | "bad_signature"
  | "missing_subject"
  | "invalid_role"
  | "mfa_required";

export type AuthResult =
  | { readonly ok: true; readonly session: OperatorSession }
  | { readonly ok: false; readonly code: AuthFailureCode };

/**
 * Adapter boundary for identity. Today the only implementation verifies an
 * HS256 JWT signed with the shared HITL_JWT_SECRET (see verifyOperatorToken).
 * TODO(owner): add an OIDC implementation (JWKS discovery, RS256/ES256,
 * issuer pinning) behind this same interface once the IdP is chosen. No fake
 * IdP is provided.
 */
export interface TokenVerifier {
  verify(token: string | null | undefined): Promise<AuthResult>;
}

const CLOCK_TOLERANCE_SEC = 5;
const MAX_SUBJECT_LENGTH = 128;
const MFA_METHOD = "mfa";

const claimsSchema = z.object({
  sub: z.string().trim().min(1).max(MAX_SUBJECT_LENGTH),
  role: z.enum(ROLES),
  amr: z.array(z.string()),
});

type VerifierConfig = Pick<AppConfig, "jwtSecret" | "jwtIssuer" | "jwtAudience">;

function fail(code: AuthFailureCode): AuthResult {
  return { ok: false, code };
}

function mapJoseError(error: unknown): AuthResult {
  if (error instanceof errors.JWTExpired) return fail("expired");
  if (error instanceof errors.JWSSignatureVerificationFailed) return fail("bad_signature");
  return fail("invalid_token");
}

function mapClaimsFailure(payload: Record<string, unknown>): AuthResult {
  if (typeof payload.sub !== "string" || payload.sub.trim().length === 0) return fail("missing_subject");
  if (typeof payload.role !== "string" || !(ROLES as readonly string[]).includes(payload.role)) {
    return fail("invalid_role");
  }
  return fail("mfa_required");
}

/**
 * Verifies the operator JWT. Every failure path returns { ok: false } (deny);
 * this function never throws for bad input.
 */
export async function verifyOperatorToken(
  token: string | null | undefined,
  config: VerifierConfig,
  now: Date = new Date(),
): Promise<AuthResult> {
  if (!token) return fail("missing_token");

  let payload: Record<string, unknown>;
  try {
    const verified = await jwtVerify(token, new TextEncoder().encode(config.jwtSecret), {
      algorithms: ["HS256"],
      currentDate: now,
      clockTolerance: CLOCK_TOLERANCE_SEC,
      requiredClaims: ["exp"],
      ...(config.jwtIssuer ? { issuer: config.jwtIssuer } : {}),
      ...(config.jwtAudience ? { audience: config.jwtAudience } : {}),
    });
    payload = verified.payload;
  } catch (error) {
    return mapJoseError(error);
  }

  const claims = claimsSchema.safeParse(payload);
  if (!claims.success || !claims.data.amr.includes(MFA_METHOD)) {
    return mapClaimsFailure(payload);
  }
  if (typeof payload.exp !== "number") return fail("invalid_token");

  return {
    ok: true,
    session: {
      sub: claims.data.sub.trim(),
      role: claims.data.role,
      amr: claims.data.amr,
      expiresAt: payload.exp,
      token,
    },
  };
}

/** Only an approver with MFA evidence may act. Viewers and non-MFA sessions never can. */
export function canAct(session: Pick<OperatorSession, "role" | "amr">): boolean {
  return session.role === "approver" && session.amr.includes(MFA_METHOD);
}

export function createHmacVerifier(config: VerifierConfig): TokenVerifier {
  return { verify: (token) => verifyOperatorToken(token, config) };
}
