"""The gateway reuses execution-motor's attestation code instead of copying it. The image copies
only the modules below; these tests fail if the gateway starts importing anything else from
``execution_motor`` (the image would then crash at start) or if the Dockerfile drifts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parents[1]
MOTOR_DIR = GATEWAY_DIR.parent / "execution-motor"
SHARED_MODULES = {
    "execution_motor",
    "execution_motor.canonical",
    "execution_motor.errors",
    "execution_motor.keys",
    "execution_motor.verifiers",
}
SHARED_FILES = {"__init__.py", "canonical.py", "errors.py", "keys.py", "verifiers.py"}


def test_gateway_imports_only_the_shared_attestation_modules() -> None:
    code = (
        "import sys\n"
        f"sys.path[:0] = [{str(GATEWAY_DIR)!r}, {str(MOTOR_DIR)!r}]\n"
        "import broker_gateway.__main__, broker_gateway.service, broker_gateway.config\n"
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] == 'execution_motor')))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert set(result.stdout.strip().split(",")) == SHARED_MODULES


def test_dockerfile_copies_exactly_the_shared_files() -> None:
    dockerfile = (GATEWAY_DIR / "Dockerfile").read_text(encoding="utf-8")
    copied = set(re.findall(r"zone-b/execution-motor/execution_motor/(\w+\.py)", dockerfile))
    assert copied == SHARED_FILES
    ignore = (GATEWAY_DIR / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    allowed = set(re.findall(r"^!zone-b/execution-motor/execution_motor/(\w+\.py)$", ignore, re.M))
    assert allowed == SHARED_FILES


def test_shared_files_are_pure_attestation_code() -> None:
    """None of the copied modules may reach broker, network or motor-pipeline code."""
    for name in SHARED_FILES:
        source = (MOTOR_DIR / "execution_motor" / name).read_text(encoding="utf-8")
        relative = set(re.findall(r"^from \.(\w+) import", source, re.M))
        assert relative <= {"errors", "verifiers", "canonical"}, (name, relative)
        assert "httpx" not in source and "grpc" not in source, name
