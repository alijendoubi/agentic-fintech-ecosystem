const NANOS_SCALE = 9;
const DIGITS_ONLY = /^-?\d+$/;

/**
 * Formats an int64 nanos decimal string (1e-9 units) exactly, using integer
 * arithmetic only: "1500000000" -> "1.50". Non-numeric input yields "n/a".
 */
export function formatNanos(value: string, fractionDigits = 2): string {
  if (!DIGITS_ONLY.test(value)) return "n/a";
  const negative = value.startsWith("-");
  const digits = (negative ? value.slice(1) : value).padStart(NANOS_SCALE + 1, "0");
  const whole = digits.slice(0, digits.length - NANOS_SCALE);
  const fraction = digits.slice(digits.length - NANOS_SCALE).padEnd(fractionDigits, "0").slice(0, fractionDigits);
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const sign = negative && /[1-9]/.test(whole + fraction) ? "-" : "";
  return fractionDigits > 0 ? `${sign}${grouped}.${fraction}` : `${sign}${grouped}`;
}

export function formatProbability(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export function formatDecimal(value: number, fractionDigits = 2): string {
  return Number.isFinite(value) ? value.toFixed(fractionDigits) : "n/a";
}

/** int64 ns decimal string to epoch milliseconds (safe: ms fits a double). */
export function nsToMs(value: string): number {
  return Number(BigInt(value) / 1_000_000n);
}

export function formatUtc(ms: number): string {
  return new Date(ms).toISOString().replace("T", " ").replace(/\.\d+Z$/, " UTC");
}

/** "mm:ss" or "h:mm:ss" for a non-negative millisecond duration. */
export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number): string => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function humanizeEnum(value: string): string {
  return value.replace(/^(REASON_|SIGNAL_)/, "").toLowerCase().replace(/_/g, " ");
}
