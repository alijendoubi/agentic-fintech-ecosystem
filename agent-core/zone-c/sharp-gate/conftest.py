"""`sharp-gate` has a hyphen in its name: put this directory on sys.path so `afe_sharp` imports
flat. The sibling
audit-logger package and its Postgres harness are TEST-TIME dependencies only (real AuditLogger +
real container)."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_AUDIT = _HERE.parent / "audit-logger"
for _p in (_HERE, _HERE / "tests", _AUDIT, _AUDIT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
