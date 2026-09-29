"""`broker-gateway` has a hyphen in its directory name, which is not a valid Python
identifier. Put this directory on sys.path so `broker_gateway` imports normally, and put
`../execution-motor` there too: the gateway reuses the motor's attestation modules
(`execution_motor.canonical`, `.verifiers`, `.keys`, `.errors`) instead of copying them. The
Dockerfile copies exactly those files (tests/test_shared_code.py keeps that list honest).
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "execution-motor"))
sys.path.insert(0, str(_HERE))
