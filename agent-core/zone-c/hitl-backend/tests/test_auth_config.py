"""JWT re-verification (contract section 3) and startup configuration."""

from __future__ import annotations

from typing import Any

import pytest

from hitl_backend.auth import Authenticator, AuthError
from hitl_backend.config import ConfigError, Settings
from tests.fakes import SECRET, token


def auth(**over: Any) -> Authenticator:
    kwargs: dict[str, Any] = {"issuer": "afe-sso", "audience": "hitl", "service_token": None}
    kwargs.update(over)
    return Authenticator(SECRET, **kwargs)


def headers(tok: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}", **extra}


def test_valid_token_yields_the_operator() -> None:
    op = auth().authenticate(headers(token(iss="afe-sso", aud="hitl")))
    assert (op.sub, op.role) == ("alice", "approver") and "mfa" in op.amr


@pytest.mark.parametrize(
    ("tok", "status", "code"),
    [
        (token(iss="afe-sso", aud="hitl", exp_in=-10), 401, "token_invalid"),
        (token(iss="other", aud="hitl"), 401, "token_invalid"),
        (token(iss="afe-sso", aud="other"), 401, "token_invalid"),
        (token(iss="afe-sso", aud="hitl", secret="x" * 40), 401, "token_invalid"),
        (token(iss="afe-sso", aud="hitl", amr=["pwd"]), 403, "mfa_required"),
        (token(iss="afe-sso", aud="hitl", role="admin"), 403, "role_not_allowed"),
    ],
)
def test_bad_tokens_are_refused(tok: str, status: int, code: str) -> None:
    with pytest.raises(AuthError) as err:
        auth().authenticate(headers(tok))
    assert (err.value.status, err.value.code) == (status, code)


def test_missing_bearer_and_service_token() -> None:
    with pytest.raises(AuthError, match="bearer"):
        auth().authenticate({})
    guarded = auth(service_token="s" * 20)
    good = token(iss="afe-sso", aud="hitl")
    with pytest.raises(AuthError) as err:
        guarded.authenticate(headers(good))
    assert err.value.code == "service_token_invalid"
    assert guarded.authenticate(headers(good, **{"X-Service-Token": "s" * 20})).sub == "alice"


BASE = {
    "HITL_ENV": "development",
    "HITL_JWT_SECRET": SECRET,
    "AEGIS_TARGET": "aegis:50051",
    "HITL_AEGIS_IDENTITY": "hitl-backend",
}
PROD = {
    **BASE,
    "HITL_ENV": "production",
    "HITL_JWT_ISSUER": "afe-sso",
    "HITL_JWT_AUDIENCE": "hitl",
    "HITL_AEGIS_TLS_CA": "/tls/ca.pem",
    "HITL_AEGIS_TLS_CERT": "/tls/c.pem",
    "HITL_AEGIS_TLS_KEY": "/tls/c.key",
    "MOTOR_TARGET": "execution-motor:50052",
    "HITL_MOTOR_TLS_CA": "/m/ca.pem",
    "HITL_MOTOR_TLS_CERT": "/m/c.pem",
    "HITL_MOTOR_TLS_KEY": "/m/c.key",
    "HITL_APPROVAL_ATTESTOR_ID": "hitl-oidc",
    "HITL_APPROVAL_ATTESTOR_KEY_FILE": "/keys/attestor.seed",
}


def test_dev_config_and_defaults() -> None:
    s = Settings.from_env(BASE)
    assert not s.production and s.aegis_tls is None and s.motor_target is None
    assert (s.listen_host, s.listen_port) == ("127.0.0.1", 8090)


def test_production_config_is_complete_and_refuses_dev_shortcuts() -> None:
    assert Settings.from_env(PROD).production
    for missing in (
        "HITL_JWT_ISSUER",
        "MOTOR_TARGET",
        "HITL_AEGIS_TLS_CA",
        "HITL_APPROVAL_ATTESTOR_ID",
        "HITL_APPROVAL_ATTESTOR_KEY_FILE",
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({k: v for k, v in PROD.items() if k != missing})
    unset_env = {k: v for k, v in PROD.items() if k != "HITL_ENV"}
    assert Settings.from_env(unset_env).production  # unset means production


@pytest.mark.parametrize(
    "over",
    [
        {"HITL_JWT_SECRET": "short"},
        {"HITL_JWT_SECRET": "change-me-in-production-change-me-in-production"},
        {"AEGIS_TARGET": ""},
        {"HITL_AEGIS_IDENTITY": ""},
        {"HITL_BACKEND_LISTEN": "nope"},
        {"HITL_AEGIS_TLS_CA": "/only/one"},
        {"HITL_FOUR_EYES_QUANTITY_THRESHOLD": "-1"},
        {"HITL_API_TOKEN": "short"},
        {"HITL_APPROVAL_ATTESTOR_ID": "hitl-oidc"},  # without its key file
    ],
)
def test_invalid_config_is_refused(over: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, **over})
