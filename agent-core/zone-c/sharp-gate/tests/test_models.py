from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from support import PROPOSER, make_proposal

from afe_sharp import (
    PIPELINE,
    AuditEntry,
    ProposalRecord,
    ProposalValidationError,
    RubricChangeProposal,
    Stage,
    StaticRoleAuthorizer,
    StoreError,
    TransitionEvent,
    normalise_identity,
)
from afe_sharp.models import fold, transition_payload

T = datetime(2026, 9, 19, tzinfo=UTC)
GATES = [s for s in PIPELINE if s is not Stage.DRAFT]
DISTINCT = ["compliance-1", "legal-1", "backtest-ci", "risk-1", "canary-ci", "release-1"]


class _Audit:
    """Audit lookup over an explicit {seq: entry} map."""

    def __init__(self, entries: dict[int, AuditEntry]) -> None:
        self.entries = entries

    def find(self, seqs: Sequence[int]) -> dict[int, AuditEntry]:
        return {s: self.entries[s] for s in seqs if s in self.entries}


def _matching_audit(history: Sequence[TransitionEvent]) -> _Audit:
    """An audit trail that holds exactly the record every event in ``history`` refers to."""
    entries: dict[int, AuditEntry] = {}
    for e in history:
        try:
            payload = transition_payload(e)
        except ValueError:  # deliberately corrupt detail: nothing to record
            continue
        assert e.audit_seq is not None
        assert e.audit_hash is not None
        entries[e.audit_seq] = AuditEntry(e.audit_seq, e.audit_hash, f"sharp.{e.kind}", e.actor,
                                          payload)  # fmt: skip
    return _Audit(entries)


def _fold(history: Sequence[TransitionEvent]) -> ProposalRecord:
    return fold(history, _matching_audit(history))


def _ev(
    version: int,
    kind: str,
    frm: Stage | None,
    to: Stage,
    detail: dict[str, Any],
    pid: str = "p-1",
    actor: str = "actor",
) -> TransitionEvent:
    return TransitionEvent(
        pid, version, kind, frm, to, actor, T, json.dumps(detail),
        audit_seq=version, audit_hash=f"{version:064x}",
    )  # fmt: skip


def _submitted() -> TransitionEvent:
    return _ev(1, "submitted", None, Stage.DRAFT, make_proposal().to_detail(), actor=PROPOSER)


def _approved(version: int, to: Stage, actor: str) -> TransitionEvent:
    frm = PIPELINE[PIPELINE.index(to) - 1]
    return _ev(version, "approved", frm, to, {"evidence_refs": [], "note": ""}, actor=actor)


def _full_history(actors: list[str]) -> list[TransitionEvent]:
    steps = enumerate(zip(GATES, actors, strict=True), start=2)
    return [_submitted(), *[_approved(version, stage, actor) for version, (stage, actor) in steps]]


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
    rec = _fold(
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
            _fold(history)
        assert name


def test_fold_promotes_only_a_fully_valid_history() -> None:
    rec = _fold(_full_history(DISTINCT))
    assert (rec.state, rec.version, rec.is_terminal) == (Stage.PROMOTED, 7, True)
    assert [a.approver_id for a in rec.approvals] == DISTINCT


def test_fold_fails_closed_on_a_forged_history_the_database_never_saw() -> None:
    """The finding: submitted + approvals with arbitrary/repeated/self actors must not fold to
    PROMOTED, even if a compromised writer got the rows into the store."""
    same = ["mallory"] * 6
    reused = ["compliance-1", "legal-1", "COMPLIANCE-1 ", "risk-1", "canary-ci", "release-1"]
    self_approved = [PROPOSER.upper(), *DISTINCT[1:]]
    bad: dict[str, list[TransitionEvent]] = {
        "one identity signs every stage": _full_history(same),
        "identity reused after normalisation": _full_history(reused),
        "proposer approves a stage": _full_history(self_approved),
        "submitted actor is not the proposer": [
            _ev(1, "submitted", None, Stage.DRAFT, make_proposal().to_detail(), actor="other"),
            _approved(2, Stage.COMPLIANCE, "compliance-1"),
        ],
        "second submitted row": [
            _submitted(),
            _ev(2, "submitted", Stage.DRAFT, Stage.DRAFT, make_proposal().to_detail()),
        ],
        "rejection without a reason": [
            _submitted(),
            _ev(2, "rejected", Stage.DRAFT, Stage.REJECTED, {}, actor="compliance-1"),
        ],
        "rejection with blank reason": [
            _submitted(),
            _ev(2, "rejected", Stage.DRAFT, Stage.REJECTED, {"reason": "  "}, actor="c-1"),
        ],
        "blank approver": [_submitted(), _approved(2, Stage.COMPLIANCE, "  ")],
        "approval into REJECTED": [
            _submitted(),
            _ev(2, "approved", Stage.DRAFT, Stage.REJECTED, {}, actor="compliance-1"),
        ],
    }
    for name, history in bad.items():
        with pytest.raises(StoreError):
            _fold(history)
        assert name


def test_fold_rejects_unparseable_or_non_object_detail() -> None:
    sub = _submitted()
    garbage = replace(_approved(2, Stage.COMPLIANCE, "c-1"), detail_json="[1]")
    broken = replace(_approved(2, Stage.COMPLIANCE, "c-1"), detail_json="{")
    broken_first = replace(sub, detail_json="{")
    for history in ([sub, garbage], [sub, broken], [broken_first]):
        with pytest.raises(StoreError):
            _fold(history)


# -- every transition must be backed by a matching audit-chain record ---------------------------


def _replace_entry(audit: _Audit, seq: int, **changes: Any) -> _Audit:
    return _Audit({**audit.entries, seq: replace(audit.entries[seq], **changes)})


def test_fold_requires_an_existing_audit_record_for_every_transition() -> None:
    history = _full_history(DISTINCT)
    good = _matching_audit(history)
    assert fold(history, good).state is Stage.PROMOTED
    missing_last = _Audit({s: e for s, e in good.entries.items() if s != 7})
    with pytest.raises(StoreError, match="no audit record"):
        fold(history, missing_last)
    with pytest.raises(StoreError, match="no audit record"):
        fold(history, _Audit({}))  # the forged-history case: rows exist, audit trail does not


def test_fold_rejects_a_row_without_an_audit_reference() -> None:
    history = _full_history(DISTINCT)
    for changes in ({"audit_seq": None}, {"audit_hash": None}, {"audit_seq": True}):
        forged = [*history[:-1], replace(history[-1], **changes)]
        with pytest.raises(StoreError, match="audit reference"):
            fold(forged, _matching_audit(history))


def test_fold_rejects_an_audit_record_that_does_not_match_the_transition() -> None:
    history = _full_history(DISTINCT)
    good = _matching_audit(history)
    other_payload = {**good.entries[7].payload, "to_state": "CANARY"}
    tampered = {
        "hash": _replace_entry(good, 7, hash="f" * 64),
        "event type": _replace_entry(good, 7, event_type="sharp.rejected"),
        "abort record": _replace_entry(good, 7, event_type="sharp.transition_aborted"),
        "actor": _replace_entry(good, 7, actor="someone-else"),
        "payload": _replace_entry(good, 7, payload=other_payload),
    }
    for name, audit in tampered.items():
        with pytest.raises(StoreError, match="does not match"):
            fold(history, audit)
        assert name


def test_fold_rejects_one_audit_record_backing_two_transitions() -> None:
    history = _full_history(DISTINCT)
    forged = [*history[:-1], replace(history[-1], audit_seq=6, audit_hash=history[-2].audit_hash)]
    with pytest.raises(StoreError, match="more than one"):
        fold(forged, _matching_audit(history))
