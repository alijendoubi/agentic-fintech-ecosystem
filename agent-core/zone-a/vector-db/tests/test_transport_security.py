"""Chroma transport security: token header, TLS options, and no plaintext in production."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest
from afe_vector_memory import MemoryConfigError, VectorMemorySettings, open_chroma_store
from tests.fake_chroma import FakeClient


class _ChromaSettings:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _install_fake_chromadb(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake `chromadb` + `chromadb.config` recording the HttpClient call; no network."""
    seen: dict[str, Any] = {}

    def http_client(**kwargs: Any) -> FakeClient:
        seen["http_client"] = kwargs
        return FakeClient()

    chromadb = ModuleType("chromadb")
    chromadb.HttpClient = http_client  # type: ignore[attr-defined]
    config = ModuleType("chromadb.config")
    config.Settings = _ChromaSettings  # type: ignore[attr-defined]
    chromadb.config = config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "chromadb", chromadb)
    monkeypatch.setitem(sys.modules, "chromadb.config", config)
    return seen


PROD = {"ENVIRONMENT": "production"}


class TestProductionRefusesPlaintext:
    def test_ssl_defaults_off_but_production_refuses_that_default(self) -> None:
        assert VectorMemorySettings.from_env({}).ssl is False
        with pytest.raises(MemoryConfigError, match="CHROMA_SSL"):
            VectorMemorySettings.from_env(PROD)

    @pytest.mark.parametrize("value", ["false", "0", "off"])
    def test_explicit_plaintext_is_refused(self, value: str) -> None:
        with pytest.raises(MemoryConfigError, match="CHROMA_SSL"):
            VectorMemorySettings.from_env({**PROD, "CHROMA_SSL": value})

    def test_environment_value_is_case_and_space_insensitive(self) -> None:
        with pytest.raises(MemoryConfigError, match="CHROMA_SSL"):
            VectorMemorySettings.from_env({"ENVIRONMENT": " Production "})

    def test_tls_is_accepted_in_production(self) -> None:
        settings = VectorMemorySettings.from_env({**PROD, "CHROMA_SSL": "true"})
        assert settings.ssl is True and settings.ssl_verify is True

    def test_disabling_certificate_verification_is_refused_in_production(self) -> None:
        env = {**PROD, "CHROMA_SSL": "true", "CHROMA_SSL_VERIFY": "false"}
        with pytest.raises(MemoryConfigError, match="CHROMA_SSL_VERIFY"):
            VectorMemorySettings.from_env(env)

    def test_plaintext_and_unverified_tls_stay_possible_outside_production(self) -> None:
        assert VectorMemorySettings.from_env({"ENVIRONMENT": "development"}).ssl is False
        env = {"CHROMA_SSL": "true", "CHROMA_SSL_VERIFY": "false"}
        assert VectorMemorySettings.from_env(env).ssl_verify is False


class TestSslVerifyOption:
    def test_accepts_a_bool_or_a_ca_bundle_path(self) -> None:
        assert VectorMemorySettings.from_env({"CHROMA_SSL_VERIFY": "true"}).ssl_verify is True
        assert VectorMemorySettings.from_env({"CHROMA_SSL_VERIFY": "False"}).ssl_verify is False
        path = "/etc/afe/chroma-ca.pem"
        assert VectorMemorySettings.from_env({"CHROMA_SSL_VERIFY": path}).ssl_verify == path

    def test_a_ca_bundle_is_valid_in_production(self) -> None:
        env = {**PROD, "CHROMA_SSL": "true", "CHROMA_SSL_VERIFY": "/etc/afe/ca.pem"}
        assert VectorMemorySettings.from_env(env).ssl_verify == "/etc/afe/ca.pem"


class TestAuthToken:
    def test_token_is_read_and_never_shown_in_repr(self) -> None:
        settings = VectorMemorySettings.from_env({"CHROMA_AUTH_TOKEN": "s3cr3t-token_value"})
        assert settings.auth_token == "s3cr3t-token_value"
        assert "s3cr3t" not in repr(settings)

    def test_blank_means_no_token(self) -> None:
        assert VectorMemorySettings.from_env({"CHROMA_AUTH_TOKEN": "  "}).auth_token is None
        assert VectorMemorySettings.from_env({}).auth_token is None

    @pytest.mark.parametrize(
        "value",
        ["two words", "line\nbreak", "tab\tbed", "x" * 5000, "ünï"],
        ids=["space", "newline", "tab", "too-long", "non-ascii"],
    )
    def test_tokens_that_could_inject_headers_are_rejected(self, value: str) -> None:
        with pytest.raises(MemoryConfigError, match="CHROMA_AUTH_TOKEN") as info:
            VectorMemorySettings.from_env({"CHROMA_AUTH_TOKEN": value})
        assert value not in str(info.value)


class TestHttpClientConstruction:
    def test_token_is_sent_as_a_bearer_header_over_tls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _install_fake_chromadb(monkeypatch)
        settings = VectorMemorySettings(
            host="chroma.internal", ssl=True, auth_token="tok-123", connect_timeout_s=5.0
        )
        open_chroma_store(settings)
        call = seen["http_client"]
        assert call["host"] == "chroma.internal"
        assert call["ssl"] is True
        assert call["headers"] == {"Authorization": "Bearer tok-123"}
        assert "chroma_server_ssl_verify" not in call["settings"].kwargs

    def test_no_token_means_no_auth_header(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen = _install_fake_chromadb(monkeypatch)
        open_chroma_store(VectorMemorySettings(connect_timeout_s=5.0))
        assert not seen["http_client"].get("headers")

    def test_a_ca_bundle_is_passed_to_the_client_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _install_fake_chromadb(monkeypatch)
        settings = VectorMemorySettings(
            ssl=True, ssl_verify="/etc/afe/ca.pem", connect_timeout_s=5.0
        )
        open_chroma_store(settings)
        client_settings = seen["http_client"]["settings"].kwargs
        assert client_settings["chroma_server_ssl_verify"] == "/etc/afe/ca.pem"
        assert client_settings["anonymized_telemetry"] is False
