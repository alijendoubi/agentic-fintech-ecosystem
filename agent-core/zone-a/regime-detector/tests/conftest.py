"""Test setup.

``regime-detector`` has a hyphen in its directory name, so it cannot be imported
as a package path. Put the directory on sys.path so ``regime_detector`` imports.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SEED = 12345


@pytest.fixture
def rng() -> np.random.Generator:
    """Seeded generator: every test that needs randomness is reproducible."""
    return np.random.default_rng(SEED)
