"""Register this hyphenated directory as the importable package `cognitive_core`.

`cognitive-core` is not a valid Python identifier, so it cannot be imported by name, and
putting the directory itself on sys.path would expose generic top-level modules
(`config`, `models`, `graph`) that collide with other packages. Instead the directory is
loaded under the unique package name `cognitive_core`, and modules use relative imports.
Deployment images should copy/install this directory as `cognitive_core` the same way.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PACKAGE_NAME = "cognitive_core"
PACKAGE_DIR = Path(__file__).resolve().parent


def _register_package() -> None:
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        paths = [Path(p).resolve() for p in getattr(existing, "__path__", [])]
        if PACKAGE_DIR not in paths:
            raise ImportError(f"{PACKAGE_NAME!r} is already bound to a different package")
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {PACKAGE_NAME!r} from {PACKAGE_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


_register_package()
