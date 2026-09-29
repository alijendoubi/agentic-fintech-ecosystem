"""End to end over a real loopback HTTP server (the terminal's view of the contract)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from hitl_backend.auth import Authenticator
from hitl_backend.http_api import build_server
from hitl_backend.service import HitlService, Policy
from tests.fakes import HOLD_ID, NOW_NS, SECRET, FakeAudit, FakeHolds, FakeRelay, held_signal, token


@pytest.fixture
def base_url(pb: dict[str, Any]) -> Iterator[str]:
    service = HitlService(
        holds=FakeHolds(pb, held_signal(pb)),
        audit=FakeAudit(),
        pb=pb,
        policy=Policy(Decimal(1000), None, 0.0, True),
        relay=FakeRelay(pb),
        clock_ns=lambda: NOW_NS,
    )
    server = build_server(
        "127.0.0.1",
        0,
        service,
        Authenticator(SECRET, issuer=None, audience=None, service_token=None),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def call(
    url: str,
    *,
    tok: str | None = None,
    body: Any = None,
    method: str = "GET",
    content_type: str = "application/json",
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - loopback test server
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_health_is_open_everything_else_needs_a_token(base_url: str) -> None:
    assert call(f"{base_url}/healthz") == (200, {"status": "ok"})
    status, body = call(f"{base_url}/v1/holds?status=pending")
    assert status == 401 and body["error"]["code"] == "token_missing"


def test_list_get_decide_round_trip(base_url: str) -> None:
    viewer, approver = token(role="viewer", sub="victor"), token()
    status, listed = call(f"{base_url}/v1/holds?status=pending", tok=viewer)
    assert status == 200 and [h["holdId"] for h in listed["holds"]] == [HOLD_ID]
    assert call(f"{base_url}/v1/holds/{HOLD_ID}", tok=viewer)[1]["hitlStatus"] == "PENDING"
    decision = {
        "decision": "APPROVE",
        "reason": "liquidity is fine today",
        "clientRequestId": str(uuid.uuid4()),
    }
    url = f"{base_url}/v1/holds/{HOLD_ID}/decisions"
    status, body = call(url, tok=viewer, body=decision, method="POST")
    assert status == 403 and body["error"]["code"] == "role_not_allowed"
    status, body = call(url, tok=approver, body=decision, method="POST")
    assert status == 200 and body["hitlStatus"] == "APPROVED"
    assert body["approvals"][0]["approverSub"] == "alice"
    assert call(f"{base_url}/v1/holds/{HOLD_ID}", tok=viewer)[1]["hitlStatus"] == "APPROVED"
    assert call(f"{base_url}/v1/holds?status=pending", tok=viewer)[1] == {"holds": []}


def test_transport_errors_use_the_contract_envelope(base_url: str) -> None:
    tok = token()
    status, body = call(
        f"{base_url}/v1/holds/{HOLD_ID}/decisions",
        tok=tok,
        body={"x": 1},
        method="POST",
        content_type="text/plain",
    )
    assert status == 415 and set(body["error"]) == {"code", "message"}
    assert call(f"{base_url}/v1/holds/bad%20id", tok=tok)[0] in (400, 404)
    assert call(f"{base_url}/v1/nope", tok=tok)[0] == 404
    assert call(f"{base_url}/v1/holds?status=final", tok=tok)[0] == 422
