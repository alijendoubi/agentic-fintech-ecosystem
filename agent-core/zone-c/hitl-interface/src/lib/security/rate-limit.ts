/**
 * Simple in-memory sliding-window rate limiter.
 *
 * LIMITATION (documented in README): state lives in one process. With several
 * replicas each keeps its own counters, so the effective limit is
 * limit x replicas; a restart clears it. Put a shared limiter (gateway or
 * Redis) in front for production-grade enforcement.
 */

export interface RateLimitResult {
  readonly allowed: boolean;
  readonly retryAfterSec: number;
}

const MAX_TRACKED_KEYS = 10_000;

export class SlidingWindowRateLimiter {
  private hits = new Map<string, readonly number[]>();

  constructor(
    private readonly limit: number,
    private readonly windowMs: number,
    private readonly now: () => number = Date.now,
  ) {}

  check(key: string): RateLimitResult {
    const now = this.now();
    const recent = (this.hits.get(key) ?? []).filter((t) => now - t < this.windowMs);
    if (recent.length >= this.limit) {
      const oldest = recent[0] ?? now;
      this.hits.set(key, recent);
      return { allowed: false, retryAfterSec: Math.max(1, Math.ceil((oldest + this.windowMs - now) / 1000)) };
    }
    this.hits.set(key, [...recent, now]);
    this.prune(now);
    return { allowed: true, retryAfterSec: 0 };
  }

  private prune(now: number): void {
    if (this.hits.size <= MAX_TRACKED_KEYS) return;
    const fresh = new Map<string, readonly number[]>();
    for (const [key, times] of this.hits) {
      const recent = times.filter((t) => now - t < this.windowMs);
      if (recent.length > 0) fresh.set(key, recent);
    }
    this.hits = fresh;
  }
}
