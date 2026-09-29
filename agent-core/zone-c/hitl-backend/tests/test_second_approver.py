"""Two-approver holds (owner decision 2026-09-29, DECISIONS row 4): two distinct OIDC subjects,
collected across requests, attested to Aegis, every step audited."""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import grpc
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from hitl_backend.attest import ApprovalAttestor, hold_approval_text
from hitl_backend.auth import Operator
from hitl_backend.config import ConfigError
from hitl_backend.service import HitlService
from tests.fakes import HOLD_ID, NANO, NOW_NS, FakeAudit, FakeHolds, FakeRelay, FakeRpcError
from tests.fakes import held_signal as make_held
from tests.test_service import policy

ALICE = Operator(sub="oidc|alice", role="approver", amr=("pwd", "mfa"))
BOB = Operator(sub="oidc|bob", role="approver", amr=("pwd", "mfa"))
CAROL = Operator(sub="oidc|carol", role="approver", amr=("pwd", "mfa"))
SEED = bytes(range(32))


def attestor() -> ApprovalAttestor:
    key = Ed25519PrivateKey.from_private_bytes(SEED)
    return ApprovalAttestor("hitl-oidc", key, issuer="https://idp.test")


class TwoEyes:
    """Every hold needs two approvals (10 shares >= threshold 5)."""

    def __init__(
        self, pb: dict[str, Any], *, attest: bool = True, held: Any = None, **pol: Any
    ) -> None:
        self.pb = pb
        self.holds = FakeHolds(pb, held if held is not None else make_held(pb))
        self.audit = FakeAudit()
        self.relay = FakeRelay(pb)
        self.clock = [NOW_NS]
        self.service = HitlService(
            holds=self.holds,
            audit=self.audit,
            pb=pb,
            policy=policy(quantity_threshold=Decimal(5), **pol),
            relay=self.relay,
            attestor=attestor() if attest else None,
            clock_ns=lambda: self.clock[0],
        )

    def decide(
        self, who: Operator, decision: str = "APPROVE", request_id: str | None = None
    ) -> tuple[int, Any]:
        body = {
            "decision": decision,
            "reason": f"{who.sub} checked the size and liquidity",
            "clientRequestId": request_id or str(uuid.uuid4()),
        }
        return self.service.decide(who, HOLD_ID, body)

    def events(self) -> list[str]:
        return [e[0] for e in self.audit.events]


def code(reply: tuple[int, Any]) -> tuple[int, str]:
    return reply[0], reply[1]["error"]["code"]


def verifies(auth: Any, *, approve: bool = True) -> bool:
    public: Ed25519PublicKey = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
    prefix = "oidc-attested:hitl-oidc:"
    assert auth.credential_ref.startswith(prefix)
    text = hold_approval_text(HOLD_ID, approve, auth.approver_id, auth.role, auth.approved_at_ns)
    try:
        public.verify(bytes.fromhex(auth.credential_ref[len(prefix) :]), text.encode())
    except Exception:  # noqa: BLE001 - InvalidSignature
        return False
    return True


def test_two_distinct_approvers_release_the_hold_with_both_attested(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    status, hold = rig.decide(ALICE)
    assert status == 200 and hold["hitlStatus"] == "PENDING" and hold["requiredApprovals"] == 2
    assert [a["approverSub"] for a in hold["approvals"]] == ["oidc|alice"]
    assert rig.holds.resolved == []  # nothing reaches Aegis on the first approval
    assert rig.events() == ["hitl.decision.first_approval"]
    first_audit = rig.audit.events[0][2]
    assert first_audit["attested_by"] == "hitl-oidc" and first_audit["idp_issuer"]
    # Both reads show the partial approval (the terminal renders "1 of 2").
    (listed,) = rig.service.list_pending()["holds"]
    assert listed["approvals"] == hold["approvals"]
    assert rig.service.get(HOLD_ID)["approvals"] == hold["approvals"]

    rig.clock[0] += NANO
    status, hold = rig.decide(BOB)
    assert status == 200 and hold["hitlStatus"] == "APPROVED"
    assert [a["approverSub"] for a in hold["approvals"]] == ["oidc|alice", "oidc|bob"]
    (resolved,) = rig.holds.resolved
    assert resolved["approve"] is True and resolved["second_approver_id"] == "oidc|bob"
    first, second = resolved["first_approval"], resolved["second_approval"]
    assert (first.approver_id, second.approver_id) == ("oidc|alice", "oidc|bob")
    assert (first.approved_at_ns, second.approved_at_ns) == (NOW_NS, NOW_NS + NANO)
    assert first.role == second.role == "operator"
    assert verifies(first) and verifies(second)
    assert resolved["note"].startswith("oidc|alice + oidc|bob: ")
    assert rig.events() == [
        "hitl.decision.first_approval",
        "hitl.decision.attempt",
        "hitl.decision.result",
    ]
    result = rig.audit.events[-1][2]
    assert result["approvers"] == ["oidc|alice", "oidc|bob"]
    assert result["first_approver"] == "oidc|alice"
    (request,) = rig.relay.requests
    assert request.context.hitl_override.operator_id == "oidc|alice+oidc|bob"
    assert rig.service.get(HOLD_ID)["hitlStatus"] == "APPROVED"


def test_the_same_subject_cannot_approve_twice(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    assert rig.decide(ALICE)[0] == 200
    assert code(rig.decide(ALICE)) == (409, "duplicate_approver")
    assert rig.holds.resolved == []
    assert rig.events()[-1] == "hitl.decision.denied"
    assert rig.decide(BOB)[1]["hitlStatus"] == "APPROVED"  # a different person still can


@pytest.mark.parametrize("rejecter", [ALICE, BOB])
def test_a_rejection_from_any_approver_is_final(pb: dict[str, Any], rejecter: Operator) -> None:
    rig = TwoEyes(pb)
    assert rig.decide(ALICE)[0] == 200
    status, hold = rig.decide(rejecter, "REJECT")
    assert status == 200 and hold["hitlStatus"] == "REJECTED"
    assert [a["decision"] for a in hold["approvals"]] == ["APPROVE", "REJECT"]
    (resolved,) = rig.holds.resolved
    assert resolved["approve"] is False and "second_approval" not in resolved
    assert code(rig.decide(CAROL)) == (409, "already_final")


def test_a_rejection_needs_no_prior_approval(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    assert rig.decide(BOB, "REJECT")[1]["hitlStatus"] == "REJECTED"


def test_a_pending_first_approval_expires_with_the_hold(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)  # hold expires at NOW + 30 s
    assert rig.decide(ALICE)[0] == 200
    rig.clock[0] = NOW_NS + 30 * NANO
    assert rig.service.list_pending()["holds"][0]["approvals"] == []
    assert "hitl.first_approval.lapsed" in rig.events()
    assert code(rig.decide(BOB)) == (410, "expired")
    assert rig.holds.resolved == []


def test_a_first_approval_is_dropped_when_aegis_no_longer_has_the_hold(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    assert rig.decide(ALICE)[0] == 200
    rig.holds.held.clear()
    assert code(rig.decide(BOB)) == (404, "not_found")
    assert rig.events()[-2] == "hitl.first_approval.lapsed"


def test_replaying_the_first_approval_is_idempotent(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    request_id = str(uuid.uuid4())
    first = rig.decide(ALICE, request_id=request_id)
    assert rig.decide(ALICE, request_id=request_id) == first
    assert rig.events() == ["hitl.decision.first_approval", "hitl.decision.replayed"]
    assert rig.decide(BOB)[1]["hitlStatus"] == "APPROVED"
    assert len(rig.holds.resolved) == 1


def test_the_cooling_period_applies_to_the_first_approval(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb, cooling_period_s=5.0)  # decided 1 s ago
    assert code(rig.decide(ALICE)) == (422, "cooling_period")
    rig.clock[0] += 5 * NANO
    assert rig.decide(ALICE)[0] == 200
    assert rig.decide(BOB)[1]["hitlStatus"] == "APPROVED"
    assert rig.holds.resolved[0]["cooling_period_enforced"] is True


def test_an_aegis_refusal_keeps_the_first_approval(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    assert rig.decide(ALICE)[0] == 200
    rig.holds.resolve_error = FakeRpcError(grpc.StatusCode.FAILED_PRECONDITION, "stale")
    assert code(rig.decide(BOB)) == (422, "aegis_refused")
    rig.holds.resolve_error = None
    status, hold = rig.decide(CAROL)
    assert status == 200 and hold["hitlStatus"] == "APPROVED"
    assert rig.holds.resolved[-1]["first_approval"].approver_id == "oidc|alice"


def test_an_audit_failure_refuses_the_first_approval(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    rig.audit.fail = True
    assert code(rig.decide(ALICE)) == (503, "audit_unavailable")
    rig.audit.fail = False
    assert rig.service.get(HOLD_ID)["approvals"] == []  # nothing was kept


def test_without_an_attestor_the_release_is_unattested(pb: dict[str, Any]) -> None:
    """Dev only (production requires the attestor): Aegis decides whether that is enough."""
    rig = TwoEyes(pb, attest=False)
    assert rig.decide(ALICE)[0] == 200
    assert rig.decide(BOB)[0] == 200
    (resolved,) = rig.holds.resolved
    assert resolved["second_approver_id"] == "oidc|bob"
    assert "second_approval" not in resolved and "first_approval" not in resolved


def test_an_unattestable_subject_is_refused(pb: dict[str, Any]) -> None:
    rig = TwoEyes(pb)
    odd = Operator(sub="a\nrole=operator", role="approver", amr=("mfa",))
    assert code(rig.decide(odd)) == (422, "subject_unusable")
    assert rig.service.get(HOLD_ID)["approvals"] == []


def test_the_canonical_text_matches_aegis() -> None:
    assert hold_approval_text("h-1", True, "oidc|a", "operator", 42) == (
        "afe-hold-v1\nhold_id=h-1\napprove=true\napprover_id=oidc|a\nrole=operator\n"
        "approved_at_ns=42\n"
    )
    assert "approve=false" in hold_approval_text("h-1", False, "a", "operator", 1)


def test_attestor_key_file(tmp_path: Path) -> None:
    good = tmp_path / "seed"
    good.write_text(SEED.hex() + "\n")
    loaded = ApprovalAttestor.from_seed_file("hitl-oidc", good, issuer=None)
    assert loaded.public_key_hex() == attestor().public_key_hex()
    bad = tmp_path / "bad"
    bad.write_text("abcd")
    with pytest.raises(ConfigError):
        ApprovalAttestor.from_seed_file("hitl-oidc", bad, issuer=None)
    with pytest.raises(ConfigError):
        ApprovalAttestor.from_seed_file("hitl-oidc", tmp_path / "missing", issuer=None)
    with pytest.raises(ConfigError):
        ApprovalAttestor("has:colon", Ed25519PrivateKey.generate(), issuer=None)
