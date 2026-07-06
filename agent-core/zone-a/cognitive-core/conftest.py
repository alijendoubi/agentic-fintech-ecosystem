"""`cognitive-core` has a hyphen in its directory name, which is not a valid
Python identifier — relative package imports (`from ..models import X`)
can't cross it. Put this directory on sys.path directly so intra-service
modules import flat (`from models import X`) instead of as a dotted package.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
