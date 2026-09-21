"use client";

import { formatDuration } from "@/lib/format";
import { useServerClock } from "./useServerClock";

interface ExpiryCountdownProps {
  readonly expiresAtMs: number;
  readonly serverNowMs: number;
}

export function ExpiryCountdown({ expiresAtMs, serverNowMs }: ExpiryCountdownProps) {
  const now = useServerClock(serverNowMs);
  const remaining = expiresAtMs - now;
  if (remaining <= 0) {
    return (
      <span className="expiry expiry-expired" role="status">
        Expired: cannot be approved
      </span>
    );
  }
  return (
    <span className="expiry" role="timer" aria-live="off">
      <span className="visually-hidden">Time remaining: </span>
      {formatDuration(remaining)}
    </span>
  );
}
