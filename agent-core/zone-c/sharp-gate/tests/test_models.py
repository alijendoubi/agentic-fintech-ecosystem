from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from support import make_proposal

from afe_sharp import (
    ProposalValidationError,
    RubricChangeProposal,
    Stage,
    StaticRoleAuthorizer,
    StoreError,
    TransitionEvent,
    normalise_identity,
)
from afe_sharp.models import fold

T = datetime(2026, 9, 19, tzinfo=UTC)


def _ev(
    version: int, kind: str, frm: Stage | None, to: Stage, detail: dict[str, Any], pid: str = "p-1"
) -> TransitionEvent:
    return TransitionEvent(pid, version, kind, frm, to, "actor", T, json.dumps(detail))


def _submitted() -> TransitionEvent:
    return _ev(1, "submitted", None, Stage.DRAFT, make_proposal().to_detail())


def test_identity_normalisation() -> None:
    assert normalise_identity("  Alice ") == normalise_identity("alice")
    assert normalise_identity("ＡＬＩＣＥ") == "alice"  # NFKC full-width
    for bad in ("", "  ", "a\x00b"):
        with pytest.raises(ProposalValidationError):
            normalise_identity(bad)


def test_proposal_validation() -> None:
    ok = make_proposal()
    assert ok.evidence_refs == ("signal:s-1", "report:r-1")
    base = ok.to_detail()
    for field, bad in (
        ("proposal_id", ""),
        ("proposer_id", " "),
        ("description", ""),
        ("proposed_change", ""),
        ("rationale", ""),
        ("evidence_refs", ()),
        ("evidence_refs", ("",)),
    ):
        with pytest.raises(ProposalValidationError):
            RubricChangeProposal(
                **{
                    **base,
                    field: bad,
                    "evidence_refs": bad
                    if field == "evidence_refs"
                    else tuple(base["evidence_refs"]),
                }
            )
    with pytest.raises(ProposalValidationError):
        RubricChangeProposal(
            **{**base, "evidence_refs": tuple(base["evidence_refs"]), "rationale": "x" * 20_001}
        )


def test_from_reflector_maps_current_zone_a_fields() -> None:
    reflector = {
        "proposal_id": "p-9",
        "trigger_signal_id": "sig-7",
        "observed_underperformance": "lost money in CRISIS",
        "proposed_change": "lower size",
        "rationale": "because",
    }
    p = RubricChangeProposal.from_reflector(reflector, proposer_id="reflector-svc")
    assert (p.proposal_id, p.proposer_id, p.description) == (
        "p-9",
        "reflector-svc",
        "lost money in CRISIS",
    )
    assert p.evidence_refs == ("signal:sig-7",)
    native = {**reflector, "description": "d", "evidence_refs": ["r-1"]}
    assert RubricChangeProposal.from_reflector(native, "svc").evidence_refs == (
        "r-1",
        "signal:sig-7",
    )
    with pytest.raises(ProposalValidationError, match="missing proposal field"):
        RubricChangeProposal.from_reflector({"proposal_id": "x"}, "svc")
    with pytest.raises(ProposalValidationError, match="evidence"):
        RubricChangeProposal.from_reflector({**reflector, "trigger_signal_id": ""}, "svc")


def test_authorizer_denies_unlisted_stages_and_identities() -> None:
    auth = StaticRoleAuthorizer({Stage.LEGAL: ["Legal-1"]})
    assert auth.is_authorized("legal-1 ", Stage.LEGAL)
    assert not auth.is_authorized("legal-1", Stage.RISK)
    assert not auth.is_authorized("other", Stage.LEGAL)


def test_fold_accepts_valid_history_and_rejects_corrupt_history() -> None:
    sub = _submitted()
    rec = fold(
        [
            sub,
            _ev(
                2, "approved", Stage.DRAFT, Stage.COMPLIANCE, {"evidence_refs": ["e"], "note": "n"}
            ),
        ]
    )
    assert (rec.state, rec.version, rec.next_stage) == (Stage.COMPLIANCE, 2, Stage.LEGAL)
    assert rec.approvals[0].note == "n"
    bad_histories = {
        "empty": [],
        "no submit": [_ev(1, "approved", Stage.DRAFT, Stage.COMPLIANCE, {})],
        "bad proposal": [_ev(1, "submitted", None, Stage.DRAFT, {"proposal_id": "p-1"})],
        "wrong start state": [_ev(1, "submitted", None, Stage.LEGAL, make_proposal().to_detail())],
        "gap": [sub, _ev(3, "approved", Stage.DRAFT, Stage.COMPLIANCE, {})],
        "stage skip": [sub, _ev(2, "approved", Stage.DRAFT, Stage.LEGAL, {})],
        "wrong from": [sub, _ev(2, "approved", Stage.LEGAL, Stage.RISK, {})],
        "unknown kind": [sub, _ev(2, "edited", Stage.DRAFT, Stage.COMPLIANCE, {})],
        "after reject": [sub, _ev(2, "rejected", Stage.DRAFT, Stage.REJECTED, {"reason": "r"}),
                         _ev(3, "approved", Stage.REJECTED, Stage.COMPLIANCE, {})],
        "other proposal": [sub, _ev(2, "approved", Stage.DRAFT, Stage.COMPLIANCE, {}, pid="p-2")],
    }  # fmt: skip
    for name, history in bad_histories.items():
        with pytest.raises(StoreError):
            fold(history)
        assert name
