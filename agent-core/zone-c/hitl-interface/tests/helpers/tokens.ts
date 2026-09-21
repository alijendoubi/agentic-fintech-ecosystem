import { SignJWT } from "jose";
import type { AppConfig } from "@/lib/config";
import { loadConfig } from "@/lib/config";

export const TEST_SECRET = "k9Xv2mQ7pL4tR8wZ1nB6cY3dF5hJ0aGs";

export function testConfig(overrides: Record<string, string | undefined> = {}): AppConfig {
  return loadConfig({
    HITL_JWT_SECRET: TEST_SECRET,
    HITL_API_BASE_URL: "http://backend.test:4000",
    NODE_ENV: "test",
    ...overrides,
  });
}

export interface TokenSpec {
  readonly sub?: string | null;
  readonly role?: string | null;
  readonly amr?: readonly string[] | null;
  readonly expiresInSec?: number;
  readonly secret?: string;
  readonly alg?: "HS256" | "HS384" | "HS512";
  readonly issuer?: string;
  readonly audience?: string;
  readonly issuedAt?: Date;
}

/** Mints a test JWT. Pass null for a claim to omit it. */
export async function mintToken(spec: TokenSpec = {}): Promise<string> {
  const claims: Record<string, unknown> = {};
  if (spec.role !== null) claims.role = spec.role ?? "approver";
  if (spec.amr !== null) claims.amr = spec.amr ?? ["pwd", "mfa"];
  const issuedAt = spec.issuedAt ?? new Date();
  const iat = Math.floor(issuedAt.getTime() / 1000);
  let jwt = new SignJWT(claims)
    .setProtectedHeader({ alg: spec.alg ?? "HS256" })
    .setIssuedAt(iat)
    .setExpirationTime(iat + (spec.expiresInSec ?? 600));
  if (spec.sub !== null) jwt = jwt.setSubject(spec.sub ?? "approver-a");
  if (spec.issuer) jwt = jwt.setIssuer(spec.issuer);
  if (spec.audience) jwt = jwt.setAudience(spec.audience);
  return jwt.sign(new TextEncoder().encode(spec.secret ?? TEST_SECRET));
}
