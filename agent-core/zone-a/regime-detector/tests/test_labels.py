"""RegimeLabel must match the shared proto enum exactly."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from regime_detector.labels import N_STATES, STATE_REGIMES, RegimeLabel

PROTO = Path(__file__).resolve().parents[3] / "shared" / "proto" / "market_snapshot.proto"


def test_unknown_is_proto_name() -> None:
    assert RegimeLabel.UNKNOWN.value == "REGIME_UNKNOWN"


def test_five_state_regimes_exclude_unknown() -> None:
    assert N_STATES == 5
    assert RegimeLabel.UNKNOWN not in STATE_REGIMES
    assert len(set(STATE_REGIMES)) == 5


@pytest.mark.skipif(not PROTO.exists(), reason="proto file not in this checkout/build context")
def test_labels_match_proto_enum() -> None:
    text = PROTO.read_text(encoding="utf-8")
    body = re.search(r"enum RegimeLabel \{(.*?)\}", text, re.S)
    assert body is not None
    names = set(re.findall(r"^\s*([A-Z_]+)\s*=\s*\d+;", body.group(1), re.M))
    assert names == {label.value for label in RegimeLabel}
