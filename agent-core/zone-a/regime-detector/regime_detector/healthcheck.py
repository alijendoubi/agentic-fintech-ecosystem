"""Container HEALTHCHECK: the service loop must have touched its heartbeat recently."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from regime_detector.config import DEFAULT_HEARTBEAT_PATH

MIN_MAX_AGE_S = 30.0


def is_healthy(path: Path, now: float, max_age_s: float) -> bool:
    """True if ``path`` exists and was modified within ``max_age_s`` seconds."""
    try:
        age = now - path.stat().st_mtime
    except OSError:
        return False
    return 0.0 <= age <= max_age_s or -1.0 <= age < 0.0


def main() -> int:
    path = Path(os.environ.get("HEARTBEAT_PATH", DEFAULT_HEARTBEAT_PATH))
    try:
        interval = float(os.environ.get("INFERENCE_INTERVAL_S", "1.0"))
    except ValueError:
        return 1
    max_age = max(MIN_MAX_AGE_S, 10.0 * interval)
    return 0 if is_healthy(path, time.time(), max_age) else 1


if __name__ == "__main__":
    sys.exit(main())
