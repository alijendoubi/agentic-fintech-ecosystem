"""REST transport (contract section 4) on the stdlib threading HTTP server.

Private-network service: the terminal's server side is the only intended client (plus an
optional service token). TODO(owner): mTLS between the terminal and this service.
"""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import structlog

from .auth import Authenticator, AuthError
from .service import ApiError, HitlService

log = structlog.get_logger("hitl_backend.http")
MAX_BODY = 16 * 1024
_HOLD = re.compile(r"^/v1/holds/([^/]+)$")
_DECISIONS = re.compile(r"^/v1/holds/([^/]+)/decisions$")


def build_server(
    host: str, port: int, service: HitlService, auth: Authenticator
) -> ThreadingHTTPServer:
    class Handler(_Handler):
        pass

    Handler.service = service
    Handler.auth = auth
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


class _Handler(BaseHTTPRequestHandler):
    service: HitlService
    auth: Authenticator
    server_version = "hitl-backend"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return  # structlog below; never log headers (bearer tokens)

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        url = urlsplit(self.path)
        if url.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        try:
            self.auth.authenticate(self.headers)
            if url.path == "/v1/holds":
                status = parse_qs(url.query).get("status", ["pending"])
                if status != ["pending"]:
                    raise ApiError(422, "invalid_request", "only status=pending is supported")
                self._send(200, self.service.list_pending())
                return
            match = _HOLD.match(url.path)
            if match is None:
                raise ApiError(404, "not_found", "no such route")
            self._send(200, self.service.get(match.group(1)))
        except (ApiError, AuthError) as err:
            self._fail(err)

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        url = urlsplit(self.path)
        try:
            operator = self.auth.authenticate(self.headers)
            match = _DECISIONS.match(url.path)
            if match is None:
                raise ApiError(404, "not_found", "no such route")
            status, body = self.service.decide(operator, match.group(1), self._json_body())
            self._send(status, body)
        except (ApiError, AuthError) as err:
            self._fail(err)

    def _json_body(self) -> Any:
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            raise ApiError(415, "invalid_request", "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError as exc:
            raise ApiError(411, "invalid_request", "Content-Length required") from exc
        if not 0 < length <= MAX_BODY:
            raise ApiError(413, "invalid_request", f"body must be 1..{MAX_BODY} bytes")
        try:
            return json.loads(self.rfile.read(length))
        except ValueError as exc:
            raise ApiError(422, "invalid_request", "body is not valid JSON") from exc

    def _fail(self, err: ApiError | AuthError) -> None:
        log.info("request_refused", path=urlsplit(self.path).path, status=err.status, code=err.code)
        self._send(err.status, {"error": {"code": err.code, "message": err.message[:500]}})

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status, HTTPStatus(status).phrase)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
