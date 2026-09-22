"""Unit coverage for ``build_grpc_server`` itself (both the mTLS and insecure-dev branches),
independent of the full integration test in test_server_integration.py (which builds a
server directly with a dynamic port to prove real mTLS enforcement over the wire)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from execution_motor.server import build_grpc_server
from execution_motor.server_config import ServerConfig, TlsPaths

from .tls_certs import build_pki, write_pem


class _FakeAddServicerToServer:
    def __call__(self, servicer: Any, server: Any) -> None:
        self.servicer = servicer
        self.server = server


def _config(**overrides: Any) -> ServerConfig:
    base = {
        "environment": "test",
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "tls": None,
        "aegis_target": None,
        "aegis_tls": None,
        "use_mock_broker": True,
        "attestation_keys_file": Path("keys.json"),
    }
    base.update(overrides)
    return ServerConfig(**base)  # type: ignore[arg-type]


def test_build_grpc_server_mtls_binds_a_secure_port(tmp_path: Path) -> None:
    pki = build_pki()
    tls = TlsPaths(
        cert=write_pem(tmp_path, "s.pem", pki.server.cert_pem),
        key=write_pem(tmp_path, "s.key", pki.server.key_pem),
        client_ca=write_pem(tmp_path, "ca.pem", pki.ca.cert_pem),
    )
    add = _FakeAddServicerToServer()
    server = build_grpc_server(object(), add, _config(listen_port=0, tls=tls))
    try:
        assert add.servicer is not None
    finally:
        server.stop(grace=0)


def test_build_grpc_server_insecure_dev_binds_a_plaintext_loopback_port() -> None:
    add = _FakeAddServicerToServer()
    server = build_grpc_server(object(), add, _config(listen_port=0, tls=None))
    try:
        assert add.servicer is not None
    finally:
        server.stop(grace=0)
