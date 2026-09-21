/**
 * Server-side configuration. Everything here is read from process.env at
 * runtime on the server only. No value in this module is ever exposed to the
 * client bundle (there are deliberately no NEXT_PUBLIC_* variables).
 *
 * loadConfig() FAILS CLOSED: any invalid or missing security-relevant setting
 * throws a ConfigError, and instrumentation.ts turns that into a refusal to
 * start.
 */

export const MIN_SECRET_LENGTH = 32;
const MIN_DISTINCT_SECRET_CHARS = 8;
const PLACEHOLDER_FRAGMENTS = [
  "change-me",
  "change_me",
  "changeme",
  "replace-me",
  "replaceme",
  "placeholder",
  "your-secret",
  "yoursecret",
  "example",
  "default",
  "password",
] as const;

const DEFAULT_API_TIMEOUT_MS = 5000;
const DEFAULT_FOUR_EYES_QUANTITY_THRESHOLD = 1000;
const DEFAULT_MUTATIONS_PER_MINUTE = 10;
const DEFAULT_LOGINS_PER_MINUTE = 10;

export type EnvSource = Readonly<Record<string, string | undefined>>;

export interface AppConfig {
  readonly jwtSecret: string;
  readonly jwtIssuer: string | null;
  readonly jwtAudience: string | null;
  readonly apiBaseUrl: string | null;
  readonly apiServiceToken: string | null;
  readonly apiTimeoutMs: number;
  readonly demoMode: boolean;
  readonly isProduction: boolean;
  readonly fourEyes: {
    /** Signals with quantity >= this need two distinct approvers. 0 = every signal. */
    readonly quantityThreshold: number;
    /** Optional notional (quantity * priceLimit, USD) threshold; limit orders only. */
    readonly notionalThresholdUsd: number | null;
  };
  readonly allowedOrigins: readonly string[];
  readonly mutationsPerMinute: number;
  readonly loginsPerMinute: number;
}

export class ConfigError extends Error {
  readonly problems: readonly string[];

  constructor(problems: readonly string[]) {
    super(`Invalid HITL configuration: ${problems.join("; ")}`);
    this.name = "ConfigError";
    this.problems = problems;
  }
}

/** Returns a description of what is wrong with the secret, or null if acceptable. Never echoes the value. */
function secretProblem(raw: string | undefined): string | null {
  const secret = raw?.trim() ?? "";
  if (secret.length === 0) {
    return "HITL_JWT_SECRET is not set";
  }
  const lowered = secret.toLowerCase();
  if (PLACEHOLDER_FRAGMENTS.some((fragment) => lowered.includes(fragment))) {
    return "HITL_JWT_SECRET looks like a placeholder value; set a real random secret";
  }
  if (secret.length < MIN_SECRET_LENGTH) {
    return `HITL_JWT_SECRET must be at least ${MIN_SECRET_LENGTH} characters`;
  }
  if (new Set(secret).size < MIN_DISTINCT_SECRET_CHARS) {
    return `HITL_JWT_SECRET must contain at least ${MIN_DISTINCT_SECRET_CHARS} distinct characters`;
  }
  return null;
}

function parseNumber(
  name: string,
  raw: string | undefined,
  fallback: number | null,
  opts: { min: number; integer: boolean },
  problems: string[],
): number | null {
  if (raw === undefined || raw.trim() === "") {
    return fallback;
  }
  const value = Number(raw);
  const valid = Number.isFinite(value) && value >= opts.min && (!opts.integer || Number.isInteger(value));
  if (!valid) {
    problems.push(`${name} must be a ${opts.integer ? "integer" : "number"} >= ${opts.min}`);
    return fallback;
  }
  return value;
}

function parseHttpUrl(name: string, raw: string | undefined, problems: string[]): string | null {
  if (raw === undefined || raw.trim() === "") {
    return null;
  }
  try {
    const url = new URL(raw.trim());
    if (url.protocol !== "http:" && url.protocol !== "https:") {
      throw new Error("unsupported protocol");
    }
    return url.toString().replace(/\/+$/, "");
  } catch {
    problems.push(`${name} must be a valid http(s) URL`);
    return null;
  }
}

function optionalString(raw: string | undefined): string | null {
  const value = raw?.trim();
  return value ? value : null;
}

function parseOrigins(raw: string | undefined): readonly string[] {
  return (raw ?? "")
    .split(",")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
}

export function loadConfig(env: EnvSource): AppConfig {
  const problems: string[] = [];
  const isProduction = env.NODE_ENV === "production";
  const demoMode = env.HITL_DEMO_MODE === "true";

  const secretIssue = secretProblem(env.HITL_JWT_SECRET);
  if (secretIssue) {
    problems.push(secretIssue);
  }
  if (demoMode && isProduction) {
    problems.push("HITL_DEMO_MODE is not allowed in production (demo mode is disabled in production builds)");
  }
  if (isProduction && optionalString(env.HITL_JWT_ISSUER) === null) {
    problems.push("HITL_JWT_ISSUER is required in production (tokens must be pinned to one issuer)");
  }
  if (isProduction && optionalString(env.HITL_JWT_AUDIENCE) === null) {
    problems.push("HITL_JWT_AUDIENCE is required in production (tokens must be pinned to this audience)");
  }

  const apiBaseUrl = parseHttpUrl("HITL_API_BASE_URL", env.HITL_API_BASE_URL, problems);
  if (!demoMode && apiBaseUrl === null && !problems.some((p) => p.startsWith("HITL_API_BASE_URL"))) {
    problems.push("HITL_API_BASE_URL is required (server-side env; never NEXT_PUBLIC)");
  }

  const apiTimeoutMs = parseNumber(
    "HITL_API_TIMEOUT_MS", env.HITL_API_TIMEOUT_MS, DEFAULT_API_TIMEOUT_MS, { min: 1, integer: true }, problems);
  const quantityThreshold = parseNumber(
    "HITL_FOUR_EYES_QUANTITY_THRESHOLD", env.HITL_FOUR_EYES_QUANTITY_THRESHOLD,
    DEFAULT_FOUR_EYES_QUANTITY_THRESHOLD, { min: 0, integer: false }, problems);
  const notionalThresholdUsd = parseNumber(
    "HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD", env.HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD,
    null, { min: 0, integer: false }, problems);
  const mutationsPerMinute = parseNumber(
    "HITL_MUTATION_RATE_LIMIT_PER_MIN", env.HITL_MUTATION_RATE_LIMIT_PER_MIN,
    DEFAULT_MUTATIONS_PER_MINUTE, { min: 1, integer: true }, problems);
  const loginsPerMinute = parseNumber(
    "HITL_LOGIN_RATE_LIMIT_PER_MIN", env.HITL_LOGIN_RATE_LIMIT_PER_MIN,
    DEFAULT_LOGINS_PER_MINUTE, { min: 1, integer: true }, problems);

  if (problems.length > 0) {
    throw new ConfigError(problems);
  }

  return {
    jwtSecret: (env.HITL_JWT_SECRET ?? "").trim(),
    jwtIssuer: optionalString(env.HITL_JWT_ISSUER),
    jwtAudience: optionalString(env.HITL_JWT_AUDIENCE),
    apiBaseUrl,
    apiServiceToken: optionalString(env.HITL_API_TOKEN),
    apiTimeoutMs: apiTimeoutMs ?? DEFAULT_API_TIMEOUT_MS,
    demoMode,
    isProduction,
    fourEyes: {
      quantityThreshold: quantityThreshold ?? DEFAULT_FOUR_EYES_QUANTITY_THRESHOLD,
      notionalThresholdUsd,
    },
    allowedOrigins: parseOrigins(env.HITL_ALLOWED_ORIGINS),
    mutationsPerMinute: mutationsPerMinute ?? DEFAULT_MUTATIONS_PER_MINUTE,
    loginsPerMinute: loginsPerMinute ?? DEFAULT_LOGINS_PER_MINUTE,
  };
}

/** Loads config from process.env. Throws ConfigError when invalid (callers must treat that as deny). */
export function getConfig(): AppConfig {
  return loadConfig(process.env);
}
