"""Lazy access to the generated protobuf modules (``bash agent-core/shared/proto/generate.sh``
writes them to
agent-core/shared/generated, which must be on PYTHONPATH; tests compile them into a temp dir)."""

from __future__ import annotations

import importlib
from types import ModuleType

from afe_manifest.errors import ManifestError


def load(name: str) -> ModuleType:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ManifestError(
            f"protobuf stubs '{name}' not importable; run "
            "agent-core/shared/proto/generate.sh and put "
            "agent-core/shared/generated on PYTHONPATH"
        ) from exc
