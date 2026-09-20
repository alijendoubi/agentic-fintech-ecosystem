from __future__ import annotations

from types import SimpleNamespace

import pytest
from factories import make_order, make_snapshot

from afe_manifest.canonical import (
    canonical_text,
    digest_of,
    digest_text,
    float_text,
    message_to_canonical,
)
from afe_manifest.errors import ManifestBuildError


def test_floats_are_encoded_as_shortest_roundtrip_strings() -> None:
    assert float_text(0.1) == "0.1"
    assert float_text(100.25) == "100.25"
    assert float_text(1e30) == "1e+30"
    assert float_text(-0.0) == "0.0"
    assert float(float_text(0.1 + 0.2)) == 0.1 + 0.2


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(bad: float) -> None:
    with pytest.raises(ManifestBuildError, match="non-finite"):
        float_text(bad)


def test_message_encoding_covers_scalars_enums_bytes_and_absent_submessages(
    pb: SimpleNamespace,
) -> None:
    order = message_to_canonical(make_order(pb))
    assert order["hsm_signature"] == "0102"
    assert order["side"] == "ORDER_BUY"
    assert order["order_type"] == "LIMIT"
    assert order["quantity"] == "10.0"
    assert order["created_at_ns"] == 3000
    manifest = message_to_canonical(pb.compliance_manifest.ComplianceManifest())
    assert manifest["order"] is None
    assert manifest["ptc_checks"] == []
    assert manifest["hard_block_triggered"] is False


def test_unknown_enum_number_is_rejected(pb: SimpleNamespace) -> None:
    order = make_order(pb)
    order.side = 77
    with pytest.raises(ManifestBuildError, match="unknown enum"):
        message_to_canonical(order)


def test_encoding_is_stable_and_sensitive(pb: SimpleNamespace) -> None:
    a = message_to_canonical(make_snapshot(pb))
    assert a == message_to_canonical(make_snapshot(pb))
    assert digest_of(a) != digest_of(message_to_canonical(make_snapshot(pb, mid_price=100.26)))


def test_canonical_text_rules() -> None:
    assert canonical_text({"b": 1, "a": "é"}) == '{"a":"\\u00e9","b":1}'
    assert digest_of("x") == digest_text(
        '"x"'
    )  # digest_of hashes the canonical JSON text of the value
    with pytest.raises(ManifestBuildError, match="not canonically serialisable"):
        canonical_text({"a": float("nan")})
    with pytest.raises(ManifestBuildError, match="not canonically serialisable"):
        canonical_text({"a": object()})
