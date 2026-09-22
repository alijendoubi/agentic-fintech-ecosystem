"""Test setup.

``refdata-bridge`` has a hyphen in its directory name, so it cannot be imported
as a package path. Put the directory on ``sys.path`` so ``refdata_bridge`` imports
(same convention as ``agent-core/zone-a/regime-detector/tests/conftest.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Generated protobuf/grpc stubs (agent-core/shared/proto/generate.sh output),
# needed by fake_aegis.py's real gRPC server/messages in the mTLS tests.
_GENERATED_DIR = Path(__file__).resolve().parents[3] / "shared" / "generated"
if _GENERATED_DIR.is_dir():
    sys.path.insert(0, str(_GENERATED_DIR))
