"""HitlService: listing, the single-approver decision flow and every contract rule it enforces."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import grpc
import pytest

from hitl_backend.auth import Operator
from hitl_backend.service import HitlService, Policy
from tests.fakes import (
    HOLD_ID,
    NANO,
    NOW_NS,
    FakeAudit,
    FakeHolds,
    FakeRelay,
    FakeRpcError,
    held_signal,
)

APPROVER = Operator(sub="alice", role="approver", amr=("pwd", "mfa"))
VIEWER = Operator(sub="victor", role="viewer", amr=("pwd", "mfa"))


def policy(**over: Any) -> Policy:
    fields: dict[str, Any] = {
        "quantity_threshold": Decimal(1000),
        "notional_threshold_usd": None,
        "cooling_period_s": 0.0,
        "unscored_dev": True,
    }
    fields.update(over)
    return Policy(**fields)


class Rig:
    def __init__(
        self, pb: dict[str, Any], *, held: Any = None, relay: bool = True, **pol: Any
    ) -> None:
        self.holds = FakeHolds(pb, held if held is not None else held_signal(pb))
        self.audit = FakeAudit()
        self.relay = FakeRelay(pb) if relay else None
        self.clock = [NOW_NS]
        self.service = HitlService(
            holds=self.holds,
            audit=self.audit,
            pb=pb,
            policy=policy(**pol),
            relay=self.relay,
            clock_ns=lambda: self.clock[0],
        )

    def decide(
        self,
        decision: str = "APPROVE",
        *,
        who: Operator = APPROVER,
        request_id: str | None = None,
        reason: str = "liquidity is fine today",
    ) -> tuple[int, Any]:
        body = {
            "decision": decision,
            "reason": reason,
            "clientRequestId": request_id or str(uuid.uuid4()),
        }
        return self.service.decide(who, HOLD_ID, body)


def code(reply: tuple[int, Any]) -> tuple[int, str]:
    return reply[0], reply[1]["error"]["code"]


def test_pending_list_matches_the_contract_shape(pb: dict[str, Any]) -> None:
    (hold,) = Rig(pb).service.list_pending()["holds"]
    assert hold["holdId"] == HOLD_ID and hold["hitlStatus"] == "PENDING"
    assert hold["side"] == "BUY" and hold["regime"] == "TRENDING_BULL"
    assert hold["quantityNanos"] == str(10 * NANO) and hold["createdAtNs"] == str(NOW_NS - NANO)
    assert hold["heldReasons"] == ["REASON_UNUSUAL_ORDER_SIZE"]
    assert hold["controls"][0] == {
        "controlId": "C05",
        "isHard": False,
        "passed": False,
        "reason": "REASON_UNUSUAL_ORDER_SIZE",
        "threshold": "5",
        "observed": "10",
        "detail": "",
    }
    assert hold["requiredApprovals"] == 1 and hold["approvals"] == []
    for key in ("createdAtNs", "validUntilNs", "holdExpiresAtNs", "priceLimitNanos"):
        assert isinstance(hold[key], str) and hold[key].lstrip("-").isdigit()


def test_approve_releases_audits_and_relays_with_the_hitl_record(pb: dict[str, Any]) -> None:
    rig = Rig(pb)
    status, hold = rig.decide()
    assert status == 200 and hold["hitlStatus"] == "APPROVED"
    assert hold["approvals"][0]["approverSub"] == "alice"
    assert hold["execution"]["relayed"] and hold["execution"]["accepted"]
    (resolved,) = rig.holds.resolved
    assert resolved["approve"] is True and resolved["note"].startswith("alice: ")
    assert (
        resolved["reverse_guardrail_distress_score"] == 0.0
        and not resolved["cooling_period_enforced"]
    )
    (request,) = rig.relay.requests  # type: ignore[union-attr]
    ctx = request.context
    assert ctx.hitl_override.operator_id == "alice" and ctx.hitl_override.decision == "approved"
    assert ctx.signal.signal_id == request.decision.signal_id
    assert (
        ctx.snapshot.symbol == "AAPL"
        and dict(ctx.model_versions)["snapshot-source"] == "hold-signal-only"
    )
    assert [e[0] for e in rig.audit.events] == ["hitl.decision.attempt", "hitl.decision.result"]
    assert all(e[1] == "alice" for e in rig.audit.events)
    assert rig.service.get(HOLD_ID)["hitlStatus"] == "APPROVED"  # final state kept for GET


def test_reject_is_final_and_needs_no_classifier(pb: dict[str, Any]) -> None:
    rig = Rig(pb, unscored_dev=False)
    status, hold = rig.decide("REJECT")
    assert status == 200 and hold["hitlStatus"] == "REJECTED" and "execution" not in hold
    assert code(rig.decide("APPROVE")) == (409, "already_final")


def test_release_denied_when_aegis_rechecks_fail(pb: dict[str, Any]) -> None:
    rig = Rig(pb)
    rig.holds.release_outcome = pb["aegis_pb2"].DECISION_REJECTED
    status, hold = rig.decide()
    assert status == 200 and hold["hitlStatus"] == "RELEASE_DENIED" and "execution" not in hold
    assert rig.relay.requests == []  # type: ignore[union-attr]


def test_idempotent_by_client_request_id(pb: dict[str, Any]) -> None:
    rig = Rig(pb)
    request_id = str(uuid.uuid4())
    first = rig.decide(request_id=request_id)
    again = rig.decide(request_id=request_id)
    assert first == again and len(rig.holds.resolved) == 1
    assert rig.audit.events[-1][0] == "hitl.decision.replayed"


def test_request_ids_are_scoped_to_the_operator(pb: dict[str, Any]) -> None:
    rig = Rig(pb)
    request_id = str(uuid.uuid4())
    assert rig.decide(who=VIEWER, request_id=request_id)[0] == 403
    status, hold = rig.decide(request_id=request_id)  # same id, different person: not a replay
    assert status == 200 and hold["hitlStatus"] == "APPROVED"


def test_four_eyes_holds_cannot_be_approved_here(pb: dict[str, Any]) -> None:
    rig = Rig(pb, quantity_threshold=Decimal(5))  # 10 shares >= 5 -> two approvers
    assert rig.service.list_pending()["holds"][0]["requiredApprovals"] == 2
    assert code(rig.decide()) == (422, "second_approver_signing_unavailable")
    assert rig.holds.resolved == []
    assert rig.audit.events[-1][0] == "hitl.decision.denied"
    assert rig.decide("REJECT")[1]["hitlStatus"] == "REJECTED"  # one rejection is enough


def test_notional_threshold_counts_for_limit_orders(pb: dict[str, Any]) -> None:
    rig = Rig(pb, notional_threshold_usd=Decimal("1000"))  # 10 * 190.5 = 1905 >= 1000
    assert code(rig.decide()) == (422, "second_approver_signing_unavailable")


def test_no_distress_classifier_refuses_approval_outside_dev(pb: dict[str, Any]) -> None:
    rig = Rig(pb, unscored_dev=False)
    assert code(rig.decide()) == (422, "distress_classifier_unavailable")
    assert rig.holds.resolved == []


def test_cooling_period(pb: dict[str, Any]) -> None:
    rig = Rig(pb, cooling_period_s=5.0)  # decided 1 s ago
    assert code(rig.decide()) == (422, "cooling_period")
    rig.clock[0] += 5 * NANO
    status, _ = rig.decide()
    assert status == 200 and rig.holds.resolved[0]["cooling_period_enforced"] is True


def test_expired_and_unknown_holds(pb: dict[str, Any]) -> None:
    rig = Rig(pb, held=held_signal(pb, expires_ns=NOW_NS))
    assert code(rig.decide()) == (410, "expired")
    empty = Rig(pb)
    empty.holds.held.clear()
    assert code(empty.decide()) == (404, "not_found")


def test_viewer_cannot_decide(pb: dict[str, Any]) -> None:
    rig = Rig(pb)
    assert code(rig.decide(who=VIEWER)) == (403, "role_not_allowed")
    assert rig.holds.resolved == []


def test_audit_failure_refuses_before_aegis_is_called(pb: dict[str, Any]) -> None:
    rig = Rig(pb)
    rig.audit.fail = True
    assert code(rig.decide()) == (503, "audit_unavailable")
    assert rig.holds.resolved == []


@pytest.mark.parametrize(
    ("grpc_code", "expected"),
    [
        (grpc.StatusCode.FAILED_PRECONDITION, (422, "aegis_refused")),
        (grpc.StatusCode.NOT_FOUND, (410, "expired")),
        (grpc.StatusCode.UNAVAILABLE, (503, "aegis_unavailable")),
    ],
)
def test_aegis_errors_map_to_contract_codes(
    pb: dict[str, Any], grpc_code: grpc.StatusCode, expected: tuple[int, str]
) -> None:
    rig = Rig(pb)
    rig.holds.resolve_error = FakeRpcError(grpc_code, "a signed second approval is required")
    request_id = str(uuid.uuid4())
    assert code(rig.decide(request_id=request_id)) == expected


def test_unknown_outcome_is_not_cached(pb: dict[str, Any]) -> None:
    rig = Rig(pb)
    rig.holds.resolve_error = FakeRpcError(grpc.StatusCode.DEADLINE_EXCEEDED)
    request_id = str(uuid.uuid4())
    assert rig.decide(request_id=request_id)[0] == 503
    rig.holds.resolve_error = None
    assert rig.decide(request_id=request_id)[0] == 200  # retried, not replayed


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "MAYBE", "reason": "long enough reason", "clientRequestId": str(uuid.uuid4())},
        {"decision": "APPROVE", "reason": "short", "clientRequestId": str(uuid.uuid4())},
        {
            "decision": "APPROVE",
            "reason": "bad\x07reason here",
            "clientRequestId": str(uuid.uuid4()),
        },
        {"decision": "APPROVE", "reason": "long enough reason", "clientRequestId": "not-a-uuid"},
        ["not", "an", "object"],
    ],
)
def test_malformed_bodies_are_422(pb: dict[str, Any], body: Any) -> None:
    rig = Rig(pb)
    with pytest.raises(Exception) as err:
        rig.service.decide(APPROVER, HOLD_ID, body)
    assert getattr(err.value, "status", None) == 422
    assert rig.holds.resolved == []


def test_relay_absent_in_dev_is_reported(pb: dict[str, Any]) -> None:
    status, hold = Rig(pb, relay=False).decide()
    assert status == 200 and hold["execution"] == {
        "relayed": False,
        "detail": "no execution-motor configured (dev)",
    }
