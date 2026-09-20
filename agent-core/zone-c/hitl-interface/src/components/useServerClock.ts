"use client";

import { useEffect, useState } from "react";

const TICK_MS = 1000;

/**
 * Returns "now" in server time: the browser clock is only used to measure
 * elapsed time since render, so a skewed operator clock cannot make an
 * expired signal look live. The server still re-checks expiry on every action.
 */
export function useServerClock(serverNowMs: number): number {
  const [offsetMs] = useState(() => serverNowMs - Date.now());
  const [now, setNow] = useState(serverNowMs);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() + offsetMs), TICK_MS);
    return () => clearInterval(id);
  }, [offsetMs]);
  return now;
}
