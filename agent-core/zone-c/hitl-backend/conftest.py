"""Put this directory on sys.path (the package dir has a hyphen) and the audit-logger package."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
