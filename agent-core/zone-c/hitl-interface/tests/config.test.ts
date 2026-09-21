import { describe, expect, it } from "vitest";
import { ConfigError, loadConfig } from "@/lib/config";

const GOOD_SECRET = "k9Xv2mQ7pL4tR8wZ1nB6cY3dF5hJ0aGs";

function env(overrides: Record<string, string | undefined> = {}) {
  return {
    HITL_JWT_SECRET: GOOD_SECRET,
    HITL_API_BASE_URL: "http://backend.internal:4000",
    HITL_JWT_ISSUER: "https://idp.example.test",
    HITL_JWT_AUDIENCE: "afe-hitl",
    NODE_ENV: "production",
    ...overrides,
  };
}

describe("loadConfig: refuse to start on weak secret", () => {
  it("throws when HITL_JWT_SECRET is unset", () => {
    expect(() => loadConfig(env({ HITL_JWT_SECRET: undefined }))).toThrow(ConfigError);
  });

  it("throws when HITL_JWT_SECRET is empty or whitespace", () => {
    expect(() => loadConfig(env({ HITL_JWT_SECRET: "" }))).toThrow(ConfigError);
    expect(() => loadConfig(env({ HITL_JWT_SECRET: "   " }))).toThrow(ConfigError);
  });

  it("throws when the secret is shorter than 32 characters", () => {
    expect(() => loadConfig(env({ HITL_JWT_SECRET: "k9Xv2mQ7pL4tR8wZ1nB6cY3dF5hJ0aG" }))).toThrow(
      /at least 32/,
    );
  });

  it.each(["change-me-in-production", "CHANGE-ME-IN-PRODUCTION-please-and-thanks-0123", "changeme-changeme-changeme-changeme"])(
    "throws on placeholder secret %s",
    (secret) => {
      expect(() => loadConfig(env({ HITL_JWT_SECRET: secret }))).toThrow(/placeholder/);
    },
  );

  it("throws on a low-entropy secret of a single repeated character", () => {
    expect(() => loadConfig(env({ HITL_JWT_SECRET: "a".repeat(40) }))).toThrow(/distinct/);
  });

  it("never includes the secret value in the error message", () => {
    try {
      loadConfig(env({ HITL_JWT_SECRET: "short-but-secretvalue" }));
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(String(error)).not.toContain("short-but-secretvalue");
    }
  });

  it("accepts a strong secret", () => {
    expect(loadConfig(env()).jwtSecret).toBe(GOOD_SECRET);
  });
});

describe("loadConfig: backend and demo mode", () => {
  it("requires HITL_API_BASE_URL outside demo mode", () => {
    expect(() => loadConfig(env({ HITL_API_BASE_URL: undefined }))).toThrow(/HITL_API_BASE_URL/);
  });

  it("rejects a non-http(s) backend URL", () => {
    expect(() => loadConfig(env({ HITL_API_BASE_URL: "ftp://x" }))).toThrow(/HITL_API_BASE_URL/);
    expect(() => loadConfig(env({ HITL_API_BASE_URL: "not a url" }))).toThrow(/HITL_API_BASE_URL/);
  });

  it("refuses demo mode in production", () => {
    expect(() => loadConfig(env({ HITL_DEMO_MODE: "true" }))).toThrow(/demo mode/i);
  });

  it("allows demo mode outside production without a backend URL", () => {
    const cfg = loadConfig(
      env({ NODE_ENV: "development", HITL_DEMO_MODE: "true", HITL_API_BASE_URL: undefined }),
    );
    expect(cfg.demoMode).toBe(true);
    expect(cfg.isProduction).toBe(false);
  });

  it("never enables demo mode implicitly", () => {
    expect(loadConfig(env({ NODE_ENV: "development" })).demoMode).toBe(false);
  });
});

describe("loadConfig: tunables", () => {
  it("applies documented defaults", () => {
    const cfg = loadConfig(env());
    expect(cfg.apiTimeoutMs).toBe(5000);
    expect(cfg.fourEyes.quantityThreshold).toBe(1000);
    expect(cfg.fourEyes.notionalThresholdUsd).toBeNull();
    expect(cfg.allowedOrigins).toEqual([]);
  });

  it("parses four-eyes thresholds and allowed origins", () => {
    const cfg = loadConfig(
      env({
        HITL_FOUR_EYES_QUANTITY_THRESHOLD: "250",
        HITL_FOUR_EYES_NOTIONAL_THRESHOLD_USD: "50000",
        HITL_ALLOWED_ORIGINS: "https://a.example, https://b.example",
      }),
    );
    expect(cfg.fourEyes).toEqual({ quantityThreshold: 250, notionalThresholdUsd: 50000 });
    expect(cfg.allowedOrigins).toEqual(["https://a.example", "https://b.example"]);
  });

  it("rejects non-numeric or negative thresholds", () => {
    expect(() => loadConfig(env({ HITL_FOUR_EYES_QUANTITY_THRESHOLD: "abc" }))).toThrow(ConfigError);
    expect(() => loadConfig(env({ HITL_FOUR_EYES_QUANTITY_THRESHOLD: "-1" }))).toThrow(ConfigError);
    expect(() => loadConfig(env({ HITL_API_TIMEOUT_MS: "0" }))).toThrow(ConfigError);
  });

  it("reports every problem at once", () => {
    try {
      loadConfig({ NODE_ENV: "production", HITL_FOUR_EYES_QUANTITY_THRESHOLD: "x" });
      expect.unreachable("should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(ConfigError);
      expect((error as ConfigError).problems.length).toBeGreaterThanOrEqual(3);
    }
  });
});
