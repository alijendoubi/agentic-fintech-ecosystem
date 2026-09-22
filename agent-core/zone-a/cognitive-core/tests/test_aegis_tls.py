"""Aegis channel security: optional mTLS from env, and no insecure channel in production."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from cognitive_core.config import ConfigError
from cognitive_core.sinks import (
    aegis_tls_from_env,
    motor_tls_from_env,
    open_aegis_channel,
    open_motor_channel,
)


class _FakeGrpc(ModuleType):
    """Records how the channel was built; no network, no real TLS."""

    def __init__(self) -> None:
        super().__init__("grpc")
        self.credentials_args: dict[str, Any] | None = None
        self.calls: list[tuple[str, str]] = []
        self.aio = SimpleNamespace(
            insecure_channel=self._insecure,
            secure_channel=self._secure,
        )

    def ssl_channel_credentials(self, **kwargs: Any) -> str:
        self.credentials_args = kwargs
        return "creds"

    def _insecure(self, target: str) -> str:
        self.calls.append(("insecure", target))
        return "insecure-channel"

    def _secure(self, target: str, credentials: Any) -> str:
        assert credentials == "creds"
        self.calls.append(("secure", target))
        return "secure-channel"


@pytest.fixture
def fake_grpc(monkeypatch: pytest.MonkeyPatch) -> _FakeGrpc:
    fake = _FakeGrpc()
    monkeypatch.setitem(sys.modules, "grpc", fake)
    return fake


def _pem(tmp_path: Path, name: str, body: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def test_dev_without_tls_env_uses_an_insecure_channel(fake_grpc: _FakeGrpc) -> None:
    assert open_aegis_channel("aegis", 50051, {}) == "insecure-channel"
    assert open_aegis_channel("aegis", 50051, {"ENVIRONMENT": "development"}) == "insecure-channel"
    assert fake_grpc.calls == [("insecure", "aegis:50051")] * 2


@pytest.mark.parametrize("value", ["production", "Production", " PRODUCTION "])
def test_production_refuses_an_insecure_channel(value: str, fake_grpc: _FakeGrpc) -> None:
    with pytest.raises(ConfigError, match="AEGIS_CLIENT_TLS_CA"):
        open_aegis_channel("aegis", 50051, {"ENVIRONMENT": value})
    assert fake_grpc.calls == []


def test_production_requires_the_ca_even_when_a_client_cert_is_given(
    tmp_path: Path, fake_grpc: _FakeGrpc
) -> None:
    env = {
        "ENVIRONMENT": "production",
        "AEGIS_CLIENT_TLS_CERT": _pem(tmp_path, "c.pem", b"cert"),
        "AEGIS_CLIENT_TLS_KEY": _pem(tmp_path, "k.pem", b"key"),
    }
    with pytest.raises(ConfigError, match="AEGIS_CLIENT_TLS_CA"):
        open_aegis_channel("aegis", 50051, env)
    assert fake_grpc.calls == []


def test_mtls_material_is_loaded_into_a_secure_channel(
    tmp_path: Path, fake_grpc: _FakeGrpc
) -> None:
    env = {
        "ENVIRONMENT": "production",
        "AEGIS_CLIENT_TLS_CA": _pem(tmp_path, "ca.pem", b"ca-bytes"),
        "AEGIS_CLIENT_TLS_CERT": _pem(tmp_path, "c.pem", b"cert-bytes"),
        "AEGIS_CLIENT_TLS_KEY": _pem(tmp_path, "k.pem", b"key-bytes"),
    }
    assert open_aegis_channel("aegis", 50051, env) == "secure-channel"
    assert fake_grpc.calls == [("secure", "aegis:50051")]
    assert fake_grpc.credentials_args == {
        "root_certificates": b"ca-bytes",
        "certificate_chain": b"cert-bytes",
        "private_key": b"key-bytes",
    }


def test_server_auth_only_tls_is_allowed_with_just_a_ca(
    tmp_path: Path, fake_grpc: _FakeGrpc
) -> None:
    env = {"AEGIS_CLIENT_TLS_CA": _pem(tmp_path, "ca.pem", b"ca-bytes")}
    assert open_aegis_channel("aegis", 50051, env) == "secure-channel"
    assert fake_grpc.credentials_args == {
        "root_certificates": b"ca-bytes",
        "certificate_chain": None,
        "private_key": None,
    }


def test_client_cert_and_key_must_be_given_together(tmp_path: Path) -> None:
    ca = _pem(tmp_path, "ca.pem", b"ca")
    cert = _pem(tmp_path, "c.pem", b"cert")
    with pytest.raises(ConfigError, match="together"):
        aegis_tls_from_env({"AEGIS_CLIENT_TLS_CA": ca, "AEGIS_CLIENT_TLS_CERT": cert})
    with pytest.raises(ConfigError, match="together"):
        aegis_tls_from_env({"AEGIS_CLIENT_TLS_CA": ca, "AEGIS_CLIENT_TLS_KEY": cert})


def test_unreadable_tls_file_is_a_config_error_that_does_not_echo_contents(
    tmp_path: Path, fake_grpc: _FakeGrpc
) -> None:
    env = {"AEGIS_CLIENT_TLS_CA": str(tmp_path / "missing.pem")}
    with pytest.raises(ConfigError, match="AEGIS_CLIENT_TLS_CA"):
        open_aegis_channel("aegis", 50051, env)
    assert fake_grpc.calls == []


def test_blank_values_count_as_unset() -> None:
    assert aegis_tls_from_env({"AEGIS_CLIENT_TLS_CA": "  ", "AEGIS_CLIENT_TLS_CERT": ""}) is None


# -- execution-motor channel: mirrors the Aegis TLS/channel tests above ------------------


def test_motor_dev_without_tls_env_uses_an_insecure_channel(fake_grpc: _FakeGrpc) -> None:
    assert open_motor_channel("execution-motor:50052", {}) == "insecure-channel"
    assert fake_grpc.calls == [("insecure", "execution-motor:50052")]


@pytest.mark.parametrize("value", ["production", "Production", " PRODUCTION "])
def test_motor_production_refuses_an_insecure_channel(value: str, fake_grpc: _FakeGrpc) -> None:
    with pytest.raises(ConfigError, match="MOTOR_CLIENT_TLS_CA"):
        open_motor_channel("execution-motor:50052", {"ENVIRONMENT": value})
    assert fake_grpc.calls == []


def test_motor_mtls_material_is_loaded_into_a_secure_channel(
    tmp_path: Path, fake_grpc: _FakeGrpc
) -> None:
    env = {
        "ENVIRONMENT": "production",
        "MOTOR_CLIENT_TLS_CA": _pem(tmp_path, "ca.pem", b"ca-bytes"),
        "MOTOR_CLIENT_TLS_CERT": _pem(tmp_path, "c.pem", b"cert-bytes"),
        "MOTOR_CLIENT_TLS_KEY": _pem(tmp_path, "k.pem", b"key-bytes"),
    }
    assert open_motor_channel("execution-motor:50052", env) == "secure-channel"
    assert fake_grpc.calls == [("secure", "execution-motor:50052")]
    assert fake_grpc.credentials_args == {
        "root_certificates": b"ca-bytes",
        "certificate_chain": b"cert-bytes",
        "private_key": b"key-bytes",
    }


def test_motor_client_cert_and_key_must_be_given_together(tmp_path: Path) -> None:
    ca = _pem(tmp_path, "ca.pem", b"ca")
    cert = _pem(tmp_path, "c.pem", b"cert")
    with pytest.raises(ConfigError, match="together"):
        motor_tls_from_env({"MOTOR_CLIENT_TLS_CA": ca, "MOTOR_CLIENT_TLS_CERT": cert})


def test_motor_blank_values_count_as_unset() -> None:
    assert motor_tls_from_env({"MOTOR_CLIENT_TLS_CA": "  ", "MOTOR_CLIENT_TLS_CERT": ""}) is None


def test_build_sink_refuses_the_insecure_channel_in_production(generated_dir: Path) -> None:
    from cognitive_core import __main__ as entrypoint
    from cognitive_core.runner_config import RunnerSettings

    env = {
        "AFE_PROTO_DIR": str(generated_dir),
        "ENVIRONMENT": "production",
        "COGNITIVE_ORDER_QUANTITY": "10",  # isolate the TLS check from the sizer check below
    }
    with pytest.raises(ConfigError, match="AEGIS_CLIENT_TLS_CA"):
        entrypoint.build_sink(RunnerSettings.from_env(env), env)
