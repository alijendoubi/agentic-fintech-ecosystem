import { ConfigError, loadConfig } from "./config";
import type { EnvSource } from "./config";

/**
 * Refuse-to-start guard. Node-runtime only (called from instrumentation.ts).
 * Throwing from an instrumentation hook does NOT stop `next start` (it logs an
 * unhandled rejection and keeps serving), so on invalid configuration we log
 * the problems (never values) and exit the process with code 1.
 */
export function enforceStartupConfig(
  env: EnvSource,
  exit: (code: number) => never = (code) => process.exit(code),
  write: (text: string) => void = (text) => void process.stderr.write(text),
): void {
  try {
    loadConfig(env);
  } catch (error) {
    const problems = error instanceof ConfigError ? error.problems : ["unexpected configuration error"];
    write(`FATAL: HITL interface refusing to start: ${problems.join("; ")}\n`);
    exit(1);
  }
}
