import { createHash } from "node:crypto";

/**
 * In-process token revocation list, populated by logout and consulted on every verification.
 *
 * LIMITATION: state lives in ONE process. With several replicas a token revoked on replica A
 * is still accepted by replica B until it expires (bounded by HITL_JWT_MAX_LIFETIME_SEC), and a
 * restart forgets every revocation. Use a shared store (Redis) or a gateway-level denylist when
 * running more than one replica; until then keep the lifetime cap short.
 */

export interface RevocableSession {
  readonly jti?: string | null;
  readonly token: string;
  /** Token `exp`, seconds since epoch. */
  readonly expiresAt: number;
}

const DEFAULT_MAX_ENTRIES = 10_000;

/** jti when the token has one, else a SHA-256 of the token itself (dev tokens carry no jti). */
export function revocationKey(session: Pick<RevocableSession, "jti" | "token">): string {
  return session.jti ? `jti:${session.jti}` : `sha256:${createHash("sha256").update(session.token).digest("hex")}`;
}

export class RevocationList {
  private readonly entries = new Map<string, number>();

  constructor(
    private readonly nowSec: () => number = () => Date.now() / 1000,
    private readonly maxEntries: number = DEFAULT_MAX_ENTRIES,
  ) {}

  get size(): number {
    return this.entries.size;
  }

  revokeSession(session: RevocableSession): void {
    this.revoke(revocationKey(session), session.expiresAt);
  }

  /** Remember `key` until `expiresAtSec`; after that the token is expired on its own. */
  revoke(key: string, expiresAtSec: number): void {
    if (this.entries.size >= this.maxEntries) this.makeRoom();
    this.entries.set(key, expiresAtSec);
  }

  isRevoked(key: string): boolean {
    const expiresAt = this.entries.get(key);
    if (expiresAt === undefined) return false;
    if (expiresAt <= this.nowSec()) {
      this.entries.delete(key);
      return false;
    }
    return true;
  }

  private makeRoom(): void {
    const now = this.nowSec();
    for (const [key, expiresAt] of this.entries) {
      if (expiresAt <= now) this.entries.delete(key);
    }
    while (this.entries.size >= this.maxEntries) {
      let soonest: string | undefined;
      let soonestAt = Infinity;
      for (const [key, expiresAt] of this.entries) {
        if (expiresAt < soonestAt) {
          soonest = key;
          soonestAt = expiresAt;
        }
      }
      if (soonest === undefined) return;
      this.entries.delete(soonest);
    }
  }
}

const globalStore = globalThis as typeof globalThis & { __hitlRevocations?: RevocationList };

/** Process-wide list shared by route handlers and server components (survives dev reloads). */
export function defaultRevocationList(): RevocationList {
  globalStore.__hitlRevocations ??= new RevocationList();
  return globalStore.__hitlRevocations;
}
