"""`audit-logger` has a hyphen in its directory name, so it cannot be imported as a dotted package.
Put this directory on sys.path so the real package `afe_audit` (and tests/ helpers) import flat."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (_HERE, _HERE / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
