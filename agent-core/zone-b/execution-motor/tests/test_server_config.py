from __future__ import annotations

import pytest

from execution_motor.errors import ConfigError
from execution_motor.server_config import AegisClientTls, ServerConfig, TlsPaths


def base_env() -> dict[str, str]:
    return {
        "MOTOR_ENV": "development",
        "MOTOR_TLS_CERT": "/tls/server.pem",
        "MOTOR_TLS_KEY": "/tls/server.key",
        "MOTOR_TLS_CLIENT_CA": "/tls/ca.pem",
        "MOTOR_USE_MOCK_BROKER": "1",
        "MOTOR_ATTESTATION_KEYS_FILE": "/cfg/keys.json",
    }


def test_valid_dev_config_parses() -> None:
    cfg = ServerConfig.from_env(base_env())
    assert cfg.environment == "development"
    assert isinstance(cfg.tls, TlsPaths)
    assert cfg.listen_addr == "0.0.0.0:50061"
    assert cfg.use_mock_broker is True
    assert cfg.aegis_target is None


def test_unset_env_is_production_and_refuses_dev_paths() -> None:
    env = base_env()
    del env["MOTOR_ENV"]
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)  # mock broker forbidden in production


def test_production_requires_alpaca_creds_when_not_mock() -> None:
    env = base_env()
    del env["MOTOR_USE_MOCK_BROKER"]
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)
    env["ALPACA_API_KEY"] = "k"
    env["ALPACA_SECRET_KEY"] = "s"
    cfg = ServerConfig.from_env(env)
    assert cfg.use_mock_broker is False


def test_each_required_tls_setting_missing_is_refused() -> None:
    for key in ("MOTOR_TLS_CERT", "MOTOR_TLS_KEY", "MOTOR_TLS_CLIENT_CA"):
        env = base_env()
        del env[key]
        with pytest.raises(ConfigError):
            ServerConfig.from_env(env)


def test_insecure_dev_requires_explicit_loopback_listen_address() -> None:
    env = base_env()
    for key in ("MOTOR_TLS_CERT", "MOTOR_TLS_KEY", "MOTOR_TLS_CLIENT_CA"):
        del env[key]
    env["MOTOR_INSECURE_DEV"] = "1"
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)  # 0.0.0.0 default must not apply to plaintext
    for bad in ("0.0.0.0:50061", "192.168.1.5:50061", "10.0.0.2:50061"):
        env["MOTOR_LISTEN_ADDR"] = bad
        with pytest.raises(ConfigError):
            ServerConfig.from_env(env)
    for ok in ("127.0.0.1:50061", "[::1]:50061"):
        env["MOTOR_LISTEN_ADDR"] = ok
        cfg = ServerConfig.from_env(env)
        assert cfg.tls is None


def test_insecure_dev_is_refused_in_production() -> None:
    env = base_env()
    for key in ("MOTOR_TLS_CERT", "MOTOR_TLS_KEY", "MOTOR_TLS_CLIENT_CA"):
        del env[key]
    env["MOTOR_ENV"] = "production"
    env["MOTOR_INSECURE_DEV"] = "1"
    env["MOTOR_LISTEN_ADDR"] = "127.0.0.1:50061"
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)


def test_mock_broker_refused_in_production() -> None:
    env = base_env()
    env["MOTOR_ENV"] = "production"
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)


def test_aegis_client_tls_required_in_production() -> None:
    env = base_env()
    env["MOTOR_ENV"] = "production"
    env["ALPACA_API_KEY"] = "k"
    env["ALPACA_SECRET_KEY"] = "s"
    del env["MOTOR_USE_MOCK_BROKER"]
    env["AEGIS_TARGET"] = "aegis:50051"
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)  # no AEGIS_CLIENT_TLS_CA
    env["AEGIS_CLIENT_TLS_CA"] = "/tls/aegis-ca.pem"
    cfg = ServerConfig.from_env(env)
    assert isinstance(cfg.aegis_tls, AegisClientTls)
    assert cfg.aegis_tls.ca is not None
    assert cfg.aegis_tls.cert is None


def test_aegis_client_cert_and_key_must_be_set_together() -> None:
    env = base_env()
    env["AEGIS_CLIENT_TLS_CA"] = "/tls/aegis-ca.pem"
    env["AEGIS_CLIENT_TLS_CERT"] = "/tls/client.pem"
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)


def test_unknown_env_and_bad_listen_addr_are_refused() -> None:
    env = base_env()
    env["MOTOR_ENV"] = "prod"
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)
    env = base_env()
    env["MOTOR_LISTEN_ADDR"] = "nonsense"
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)


def test_attestation_keys_file_required() -> None:
    env = base_env()
    del env["MOTOR_ATTESTATION_KEYS_FILE"]
    with pytest.raises(ConfigError):
        ServerConfig.from_env(env)
