from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from factories import (
    make_hitl,
    make_inputs,
    make_order,
    make_ptc,
    make_signal,
    make_snapshot,
)

from afe_manifest import (
    GENESIS_HASH,
    RETENTION_YEARS,
    CognitiveOutputs,
    ManifestBuildError,
    build_manifest,
    verify,
    verify_document,
)
from afe_manifest.canonical import digest_of, message_to_canonical
from afe_manifest.errors import ManifestError
from afe_manifest.retention import add_years, format_ns_utc, retain_until_ns

T0 = int(datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC).timestamp()) * 1_000_000_000


def _build(pb: SimpleNamespace, **overrides: Any) -> Any:
    return build_manifest(
        make_inputs(pb, **overrides), clock_ns=lambda: T0, id_factory=lambda: "m-1"
    )


def test_build_assembles_all_inputs_into_the_proto(pb: SimpleNamespace) -> None:
    built = _build(pb)
    m = built.proto
    assert (m.manifest_id, m.prev_manifest_hash, m.created_at_ns) == ("m-1", GENESIS_HASH, T0)
    assert m.snapshot.symbol == "TEST"
    assert m.signal.signal_id == "sig-1"
    assert m.order.order_id == "ord-1"
    assert [c.check_name for c in m.ptc_checks] == ["hard_1", "soft_1"]
    assert (m.omega, m.expected_value) == (0.8, 1.5)
    assert (m.blue_node_thesis, m.red_node_challenge, m.judge_synthesis) == ("blue", "red", "judge")
    assert list(m.compression_summaries) == ["s1", "s2"]
    assert not m.hard_block_triggered
    assert not m.soft_block_triggered
    assert not m.hitl_override_occurred
    doc = built.document
    assert doc["model_versions"] == {"blue_node": "test-model-a@1", "judge": "test-model-b@2"}
    assert doc["manifest_digest"] == built.digest


def test_builder_is_deterministic_and_input_order_independent(pb: SimpleNamespace) -> None:
    a = _build(pb, model_versions={"a": "1", "b": "2"})
    b = _build(pb, model_versions={"b": "2", "a": "1"})
    assert a.digest == b.digest
    assert a.document_json == b.document_json


def test_digest_changes_with_every_input(pb: SimpleNamespace) -> None:
    base = _build(pb)
    variants = {
        "snapshot": _build(pb, snapshot=make_snapshot(pb, mid_price=101.0)),
        "signal": _build(pb, signal=make_signal(pb, omega=0.7)),
        "order": _build(pb, order=make_order(pb, quantity=11.0)),
        "model_versions": _build(
            pb, model_versions={"blue_node": "test-model-a@2", "judge": "x@2"}
        ),
        "ptc_checks": _build(pb, ptc_checks=[make_ptc(pb, "other")]),
        "cognitive_outputs": _build(pb, cognitive=CognitiveOutputs("blue!", "red", "judge")),
    }
    for section, built in variants.items():
        assert built.digest != base.digest, section
        assert built.document["input_digests"][section] != base.document["input_digests"][section]
    untouched = variants["snapshot"].document["input_digests"]
    for other in ("order", "model_versions", "ptc_checks", "signal"):
        assert untouched[other] == base.document["input_digests"][other], other


def test_input_digests_are_sha256_of_canonical_sections(pb: SimpleNamespace) -> None:
    built = _build(pb)
    doc = built.document
    assert doc["input_digests"]["snapshot"] == digest_of(message_to_canonical(make_snapshot(pb)))
    assert doc["input_digests"]["order"] == digest_of(message_to_canonical(make_order(pb)))


def test_verify_passes_for_a_fresh_manifest(pb: SimpleNamespace) -> None:
    report = verify(_build(pb))
    assert report.ok, report.problems


def test_retention_metadata_is_created_plus_seven_years(pb: SimpleNamespace) -> None:
    ret = _build(pb).document["retention"]
    assert ret["years"] == RETENTION_YEARS == 7
    assert ret["retain_until"] == "2033-09-19T12:00:00.000000Z"
    assert ret["retain_until_ns"] == T0 + 2557 * 86_400 * 1_000_000_000  # 7y incl. two leap days
    assert "agent-core/README.md" in ret["basis"]


def test_shorter_retention_is_refused(pb: SimpleNamespace) -> None:
    with pytest.raises(ManifestBuildError, match="retention_years"):
        build_manifest(make_inputs(pb), retention_years=6)


def test_leap_day_start_never_shortens_retention() -> None:
    leap = datetime(2028, 2, 29, 10, 0, tzinfo=UTC)
    assert add_years(leap, 7) == datetime(2035, 3, 1, 10, 0, tzinfo=UTC)
    ns = int(leap.timestamp()) * 1_000_000_000 + 123
    assert retain_until_ns(ns) % 1000 == 123
    assert format_ns_utc(retain_until_ns(ns)).startswith("2035-03-01T10:00:00")


def _tamper(built: Any, mutate: Any) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(built.document_json)
    mutate(doc)
    return doc


@pytest.mark.parametrize(
    ("name", "mutate", "expected"),
    [
        (
            "snapshot",
            lambda d: d["manifest"]["snapshot"].update(mid_price="999.0"),
            "input digest mismatch: snapshot",
        ),
        (
            "order",
            lambda d: d["manifest"]["order"].update(quantity="99.0"),
            "input digest mismatch: order",
        ),
        (
            "model",
            lambda d: d["model_versions"].update(judge="evil"),
            "input digest mismatch: model_versions",
        ),
        (
            "ptc",
            lambda d: d["manifest"]["ptc_checks"][0].update(passed=False),
            "input digest mismatch: ptc_checks",
        ),
        ("digest", lambda d: d.update(manifest_digest="0" * 64), "manifest_digest mismatch"),
        ("retention", lambda d: d["retention"].update(years=1), "below the required"),
        (
            "retain_until",
            lambda d: d["retention"].update(retain_until_ns=1),
            "not created_at + years",
        ),
        ("prev_hash", lambda d: d["manifest"].update(prev_manifest_hash="zz"), "not a sha256"),
        ("missing", lambda d: d.pop("input_digests"), "missing key: input_digests"),
        ("schema", lambda d: d.update(schema_version=99), "unsupported schema_version"),
    ],
)
def test_verify_document_detects_tampering(
    pb: SimpleNamespace, name: str, mutate: Any, expected: str
) -> None:
    report = verify_document(_tamper(_build(pb), mutate))
    assert not report.ok, name
    assert any(expected in p for p in report.problems), report.problems


def test_verify_detects_proto_document_divergence(pb: SimpleNamespace) -> None:
    built = _build(pb)
    built.proto.signal.omega = 0.1  # proto mutated after build
    report = verify(built)
    assert not report.ok
    assert any("protobuf message differs" in p for p in report.problems)


@pytest.mark.parametrize(
    ("label", "overrides", "message"),
    [
        ("no ptc", {"ptc_checks": []}, "pre-trade-control results are required"),
        ("no models", {"model_versions": {}}, "model_versions"),
        ("blank model", {"model_versions": {"a": " "}}, "non-empty"),
        ("bad prev", {"prev_manifest_hash": "abc"}, "prev_manifest_hash"),
        ("symbol mismatch", {"signal_kw": {"symbol": "OTHER"}}, "same non-empty symbol"),
        ("order/signal mismatch", {"order_kw": {"signal_id": "sig-X"}}, "does not belong"),
        ("stale snapshot", {"snapshot_kw": {"is_stale": True}}, "stale"),
        (
            "too many summaries",
            {"cognitive": CognitiveOutputs(compression_summaries=("a",) * 4)},
            "at most 3",
        ),
        ("wrong type", {"snapshot": "not a message"}, "MarketSnapshot"),
    ],
)
def test_inconsistent_inputs_are_refused(
    pb: SimpleNamespace, label: str, overrides: dict[str, Any], message: str
) -> None:
    overrides = dict(overrides)
    if "signal_kw" in overrides:
        overrides["signal"] = make_signal(pb, **overrides.pop("signal_kw"))
    if "order_kw" in overrides:
        overrides["order"] = make_order(pb, **overrides.pop("order_kw"))
    if "snapshot_kw" in overrides:
        overrides["snapshot"] = make_snapshot(pb, **overrides.pop("snapshot_kw"))
    with pytest.raises(ManifestBuildError, match=message):
        _build(pb, **overrides)


def test_ptc_type_and_values_are_validated(pb: SimpleNamespace) -> None:
    unknown = pb.compliance_manifest.PTCCheckResult(check_name="x", passed=True)
    with pytest.raises(ManifestBuildError, match="HARD/SOFT"):
        _build(pb, ptc_checks=[unknown])
    nameless = make_ptc(pb, "")
    with pytest.raises(ManifestBuildError, match="check_name"):
        _build(pb, ptc_checks=[nameless])
    bad = make_ptc(pb, "nan")
    bad.actual_value = float("nan")
    with pytest.raises(ManifestBuildError, match="non-finite"):
        _build(pb, ptc_checks=[bad])


def test_hard_block_forbids_an_order_but_allows_a_blocked_record(pb: SimpleNamespace) -> None:
    failed_hard = [make_ptc(pb, "limit", hard=True, passed=False)]
    with pytest.raises(ManifestBuildError, match="hard-block"):
        _build(pb, ptc_checks=failed_hard)
    blocked = _build(pb, ptc_checks=failed_hard, order=None)
    assert blocked.proto.hard_block_triggered
    assert not blocked.proto.HasField("order")
    assert blocked.document["manifest"]["order"] is None
    assert verify(blocked).ok


def test_soft_block_requires_an_approved_hitl_record_for_an_order(pb: SimpleNamespace) -> None:
    soft_fail = [make_ptc(pb, "hard_1"), make_ptc(pb, "soft_1", hard=False, passed=False)]
    with pytest.raises(ManifestBuildError, match="approved HITL"):
        _build(pb, ptc_checks=soft_fail)
    with pytest.raises(ManifestBuildError, match="HITL rejected"):
        _build(pb, ptc_checks=soft_fail, hitl_override=make_hitl(pb, "rejected"))
    ok = _build(pb, ptc_checks=soft_fail, hitl_override=make_hitl(pb, "approved"))
    assert ok.proto.soft_block_triggered
    assert ok.proto.soft_block_reason == "soft_1 failed"
    assert ok.proto.hitl_override_occurred
    assert ok.proto.hitl_override.operator_id == "op-test"


def test_hitl_record_is_validated(pb: SimpleNamespace) -> None:
    with pytest.raises(ManifestBuildError, match="operator_id and decision"):
        _build(pb, hitl_override=make_hitl(pb, "maybe"))
    with pytest.raises(ManifestBuildError, match="operator_id and decision"):
        _build(pb, hitl_override=make_hitl(pb, "approved", operator=""))
    with pytest.raises(ManifestBuildError, match="HITL rejected"):
        _build(pb, hitl_override=make_hitl(pb, "rejected"))


def test_clock_and_id_are_validated(pb: SimpleNamespace) -> None:
    with pytest.raises(ManifestBuildError, match="non-positive"):
        build_manifest(make_inputs(pb), clock_ns=lambda: 0)
    with pytest.raises(ManifestBuildError, match="manifest_id"):
        build_manifest(make_inputs(pb), clock_ns=lambda: T0, id_factory=lambda: " ")
    auto = build_manifest(make_inputs(pb))
    assert len(auto.manifest_id) == 36


def test_judge_synthesis_falls_back_to_signal_debate_summary(pb: SimpleNamespace) -> None:
    built = _build(pb, cognitive=CognitiveOutputs())
    assert built.proto.judge_synthesis == "judge summary"


def test_stubs_missing_fails_closed() -> None:
    from afe_manifest import stubs

    with pytest.raises(ManifestError, match="not importable"):
        stubs.load("definitely_not_a_module_pb2")
