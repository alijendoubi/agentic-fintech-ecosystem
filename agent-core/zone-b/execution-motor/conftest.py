"""`execution-motor` has a hyphen in its directory name, which is not a valid
Python identifier. Put this directory on sys.path so the real package,
`execution_motor`, imports normally (`from execution_motor.models import ...`).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
