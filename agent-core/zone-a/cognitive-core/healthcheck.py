"""Container HEALTHCHECK: `python -m cognitive_core.healthcheck` exits 0 only on HTTP 200.

Uses urllib (no `requests` dependency) and, unlike `requests.get`, treats a 503 as failure.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

from .health import HEALTH_PATH

DEFAULT_PORT = "8080"
TIMEOUT_S = 5.0


def probe(port: str) -> bool:
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:  # noqa: S310 - fixed http URL
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def main() -> int:
    return 0 if probe(os.environ.get("COGNITIVE_HEALTH_PORT", DEFAULT_PORT)) else 1


if __name__ == "__main__":
    sys.exit(main())
