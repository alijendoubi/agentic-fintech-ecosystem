"""In-memory fake of the Alpaca paper REST API for the e2e (an ``httpx.MockTransport`` handler,
never networked). It sits behind the REAL broker gateway, so every order in the e2e crosses:
motor (verify) -> GatewayTransport -> gateway (verify again, add credentials) -> here.

Response shapes follow the motor's ASSUMED Alpaca v2 shapes (execution_motor/alpaca_parse.py).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx

API_KEY = "PKE2EGATEWAYKEY01"
SECRET = "E2EGATEWAYSECRET0002"


class FakeAlpacaPaper:
    """Fills every accepted order at its limit price (all, or ``fill_fraction`` of it).

    Broker-side idempotency on client_order_id like a real broker: a second submit with the
    same id is a hard error, so a motor or gateway bug would surface in the e2e. Every request
    must carry the gateway's credentials.
    """

    def __init__(
        self, *, equity: Decimal = Decimal("100000"), fill_fraction: Decimal = Decimal(1)
    ) -> None:
        self.equity = equity
        self.fill_fraction = fill_fraction
        self.submitted: list[dict[str, str]] = []
        self.requests: list[httpx.Request] = []
        self._orders: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if (
            request.headers.get("APCA-API-KEY-ID") != API_KEY
            or request.headers.get("APCA-API-SECRET-KEY") != SECRET
        ):
            return httpx.Response(401, json={"message": "unauthorized"})
        path = request.url.path
        if request.method == "POST" and path == "/v2/orders":
            return self._submit(json.loads(request.content))
        if request.method == "GET" and path == "/v2/orders:by_client_order_id":
            order = self._orders.get(request.url.params["client_order_id"])
            return httpx.Response(200, json=order) if order else httpx.Response(404, json={})
        if request.method == "GET" and path == "/v2/orders":
            live = [o for o in self._orders.values() if o["status"] in ("new", "partially_filled")]
            return httpx.Response(200, json=live)
        if request.method == "DELETE" and path.startswith("/v2/orders/"):
            return httpx.Response(204)
        if request.method == "GET" and path == "/v2/positions":
            return httpx.Response(200, json=[])
        if request.method == "GET" and path == "/v2/account":
            eq = str(self.equity)
            return httpx.Response(
                200,
                json={
                    "status": "ACTIVE",
                    "buying_power": eq,
                    "cash": eq,
                    "equity": eq,
                    "trading_blocked": False,
                    "account_blocked": False,
                },
            )
        raise AssertionError(f"unexpected broker call {request.method} {path}")

    def _submit(self, body: dict[str, str]) -> httpx.Response:
        cid = body["client_order_id"]
        if cid in self._orders:
            raise AssertionError(f"duplicate client_order_id at broker: {cid}")
        self.submitted.append(body)
        qty = Decimal(body["qty"])
        filled = qty * self.fill_fraction
        price = body.get("limit_price")
        order = {
            "id": f"mock-{len(self._orders) + 1}",
            "client_order_id": cid,
            "symbol": body["symbol"],
            "qty": body["qty"],
            "filled_qty": str(filled),
            "filled_avg_price": price if filled > 0 else None,
            "status": "filled" if filled == qty else "partially_filled",
            "submitted_at": "2026-09-20T10:00:00Z",
            "filled_at": "2026-09-20T10:00:00.5Z",
        }
        self._orders[cid] = order
        return httpx.Response(200, json=order)
