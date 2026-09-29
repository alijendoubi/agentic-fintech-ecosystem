"""Operator JWT re-verification (contract section 3). The token is authoritative for identity."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

ROLES = frozenset({"approver", "viewer"})
_MAX_SUB = 128


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Operator:
    sub: str
    role: str
    amr: tuple[str, ...]


class Authenticator:
    def __init__(
        self,
        secret: str,
        *,
        issuer: str | None,
        audience: str | None,
        service_token: str | None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._service_token = service_token
        self._clock = clock

    def authenticate(self, headers: Any) -> Operator:
        """Verify service token (if configured) and the operator bearer token. Raises AuthError."""
        if self._service_token is not None:
            given = headers.get("X-Service-Token") or ""
            if not hmac.compare_digest(given.encode(), self._service_token.encode()):
                raise AuthError(401, "service_token_invalid", "service token missing or invalid")
        header = headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme != "Bearer" or not token.strip():
            raise AuthError(401, "token_missing", "bearer token required")
        options: dict[str, Any] = {"require": ["exp", "sub"]}
        kwargs: dict[str, Any] = {"algorithms": ["HS256"], "options": options}
        if self._issuer is not None:
            kwargs["issuer"] = self._issuer
        if self._audience is not None:
            kwargs["audience"] = self._audience
        else:
            options["verify_aud"] = False
        try:
            claims = jwt.decode(token.strip(), self._secret, **kwargs)
        except jwt.PyJWTError as exc:
            raise AuthError(401, "token_invalid", f"token rejected: {type(exc).__name__}") from exc
        sub, role, amr = claims.get("sub"), claims.get("role"), claims.get("amr")
        if not isinstance(sub, str) or not sub.strip() or len(sub) > _MAX_SUB:
            raise AuthError(401, "token_invalid", "token has no usable sub")
        if role not in ROLES:
            raise AuthError(403, "role_not_allowed", "role must be approver or viewer")
        if not isinstance(amr, list) or "mfa" not in amr:
            raise AuthError(403, "mfa_required", "multi-factor authentication is required")
        return Operator(sub=sub, role=role, amr=tuple(str(a) for a in amr))
