import { describe, expect, it } from "vitest";
import { formatDuration, formatNanos, formatProbability, formatUtc, humanizeEnum, nsToMs } from "@/lib/format";

describe("formatNanos", () => {
  it.each([
    ["1500000000", 2, "1.50"],
    ["0", 2, "0.00"],
    ["1", 9, "0.000000001"],
    ["123456789012345678", 2, "123,456,789.01"],
    ["-2500000000", 2, "-2.50"],
    ["-1", 2, "0.00"],
    ["100000000000", 0, "100"],
  ])("formats %s with %i digits as %s", (input, digits, expected) => {
    expect(formatNanos(input, digits)).toBe(expected);
  });
  it("returns n/a for non-integer input rather than guessing", () => {
    expect(formatNanos("1.5")).toBe("n/a");
    expect(formatNanos("abc")).toBe("n/a");
  });
});

describe("other formatters", () => {
  it("formats probability, durations, enums and timestamps", () => {
    expect(formatProbability(0.615)).toBe("61.5%");
    expect(formatDuration(65_000)).toBe("01:05");
    expect(formatDuration(3_725_000)).toBe("1:02:05");
    expect(formatDuration(-5)).toBe("00:00");
    expect(humanizeEnum("REASON_UNUSUAL_ORDER_SIZE")).toBe("unusual order size");
    expect(nsToMs("1758369600000000000")).toBe(1758369600000);
    expect(formatUtc(0)).toBe("1970-01-01 00:00:00 UTC");
  });
});
