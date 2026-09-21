import { describe, expect, it, vi } from "vitest";
import { enforceStartupConfig } from "@/lib/startup-guard";

const GOOD = {
  HITL_JWT_SECRET: "k9Xv2mQ7pL4tR8wZ1nB6cY3dF5hJ0aGs",
  HITL_API_BASE_URL: "https://backend.internal:4000",
  HITL_JWT_ISSUER: "https://idp.example.test",
  HITL_JWT_AUDIENCE: "afe-hitl",
  NODE_ENV: "production",
};

describe("enforceStartupConfig", () => {
  it("does nothing with a valid configuration", () => {
    const exit = vi.fn();
    const write = vi.fn();
    enforceStartupConfig(GOOD, exit as unknown as (code: number) => never, write);
    expect(exit).not.toHaveBeenCalled();
    expect(write).not.toHaveBeenCalled();
  });

  it.each([
    ["unset secret", { ...GOOD, HITL_JWT_SECRET: undefined }],
    ["short secret", { ...GOOD, HITL_JWT_SECRET: "short" }],
    ["placeholder secret", { ...GOOD, HITL_JWT_SECRET: "change-me-in-production" }],
    ["demo mode in production", { ...GOOD, HITL_DEMO_MODE: "true" }],
  ])("exits non-zero for %s and never prints the secret", (_name, env) => {
    const exit = vi.fn();
    const write = vi.fn();
    enforceStartupConfig(env, exit as unknown as (code: number) => never, write);
    expect(exit).toHaveBeenCalledWith(1);
    const output = write.mock.calls.map((c) => String(c[0])).join("");
    expect(output).toMatch(/refusing to start/i);
    expect(output).not.toContain("change-me-in-production");
  });
});
