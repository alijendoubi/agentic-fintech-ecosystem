import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { SESSION_COOKIE, extractToken } from "@/lib/auth/identity";
import { verifyOperatorToken } from "@/lib/auth/verify";
import type { OperatorSession } from "@/lib/auth/verify";
import { getRuntime } from "./runtime";

/** Verifies the operator for a server component. Any failure redirects to /login (deny). */
export async function requireSession(): Promise<OperatorSession> {
  const { config } = getRuntime();
  const [cookieStore, headerStore] = await Promise.all([cookies(), headers()]);
  const token = extractToken({
    authorization: headerStore.get("authorization"),
    cookie: cookieStore.get(SESSION_COOKIE)?.value,
  });
  const result = await verifyOperatorToken(token, config);
  if (!result.ok) redirect("/login");
  return result.session;
}
