import { HttpHitlApiClient } from "@/lib/api/http-client";
import type { HitlApiClient } from "@/lib/api/types";
import { getConfig } from "@/lib/config";
import type { AppConfig } from "@/lib/config";
import { createDemoClient } from "@/lib/demo";
import { SlidingWindowRateLimiter } from "@/lib/security/rate-limit";

const WINDOW_MS = 60_000;

export interface Runtime {
  readonly config: AppConfig;
  readonly client: HitlApiClient;
  readonly mutationLimiter: SlidingWindowRateLimiter;
  readonly loginLimiter: SlidingWindowRateLimiter;
}

const globalStore = globalThis as typeof globalThis & { __hitlRuntime?: Runtime };

function buildClient(config: AppConfig): HitlApiClient {
  if (config.demoMode) return createDemoClient(config, Date.now());
  if (!config.apiBaseUrl) throw new Error("HITL_API_BASE_URL is required");
  return new HttpHitlApiClient({
    baseUrl: config.apiBaseUrl,
    timeoutMs: config.apiTimeoutMs,
    serviceToken: config.apiServiceToken,
  });
}

/** Process-wide singletons (rate limiter and demo state must survive across requests and dev reloads). */
export function getRuntime(): Runtime {
  const existing = globalStore.__hitlRuntime;
  if (existing) return existing;
  const config = getConfig();
  const runtime: Runtime = {
    config,
    client: buildClient(config),
    mutationLimiter: new SlidingWindowRateLimiter(config.mutationsPerMinute, WINDOW_MS),
    loginLimiter: new SlidingWindowRateLimiter(config.loginsPerMinute, WINDOW_MS),
  };
  globalStore.__hitlRuntime = runtime;
  return runtime;
}
