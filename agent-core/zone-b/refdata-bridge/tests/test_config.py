"""``config.py``: env parsing and the production-refuses-plaintext gate."""

from __future__ import annotations

import pytest

from refdata_bridge.config import ConfigError, Settings


def env(**overrides: str) -> dict[str, str]:
    base = {"AEGIS_TARGET": "aegis:50051"}
    return {**base, **overrides}


def test_defaults_are_sane() -> None:
    settings = Settings.from_env(env())
    assert settings.snapshot_channel == "sensory:snapshots"
    assert settings.regime_channel == "regime:labels"
    assert settings.environment == "development"
    assert settings.tls_ca is None


def test_production_without_ca_is_refused() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env(ENVIRONMENT="production"))


def test_production_with_ca_is_accepted() -> None:
    settings = Settings.from_env(env(ENVIRONMENT="production", REFDATA_CLIENT_TLS_CA="ca.pem"))
    assert settings.environment == "production"
    assert settings.tls_ca is not None
    assert settings.tls_ca.name == "ca.pem"


def test_cert_without_key_is_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env(REFDATA_CLIENT_TLS_CERT="/c.pem"))


def test_key_without_cert_is_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env(REFDATA_CLIENT_TLS_KEY="/k.pem"))


def test_batch_interval_out_of_range_is_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env(BATCH_INTERVAL_S="0"))


def test_empty_aegis_target_is_rejected() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env(AEGIS_TARGET="  "))
