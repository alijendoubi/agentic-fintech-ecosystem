"""Test doubles: an ephemeral mTLS CA/cert pair (via ``cryptography``) and a real,
loopback ``Aegis.PushReferenceData`` gRPC server backed by an in-memory store, so
``test_aegis_client.py`` exercises the real wire protocol and real mTLS instead of
mocking the gRPC stub away.
"""

from __future__ import annotations

import datetime
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import aegis_pb2
import aegis_pb2_grpc
import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@dataclass
class TlsMaterial:
    ca_cert_pem: bytes
    server_cert_pem: bytes
    server_key_pem: bytes
    client_cert_pem: bytes
    client_key_pem: bytes


def _self_signed_ca() -> tuple[bytes, bytes, rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "refdata-bridge-test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, key_pem, key, cert


def _leaf_cert(
    common_name: str, ca_key: rsa.RSAPrivateKey, ca_cert: x509.Certificate
) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


def generate_mtls_material() -> TlsMaterial:
    """A fresh, in-memory CA + server + client (CN=``market-data``) cert chain."""
    ca_pem, ca_key_pem, ca_key, ca_cert = _self_signed_ca()
    server_cert_pem, server_key_pem = _leaf_cert("localhost", ca_key, ca_cert)
    client_cert_pem, client_key_pem = _leaf_cert("market-data", ca_key, ca_cert)
    return TlsMaterial(
        ca_cert_pem=ca_pem,
        server_cert_pem=server_cert_pem,
        server_key_pem=server_key_pem,
        client_cert_pem=client_cert_pem,
        client_key_pem=client_key_pem,
    )


def write_tls_files(material: TlsMaterial, directory: Path) -> dict[str, Path]:
    paths = {
        "ca": directory / "ca.pem",
        "client_cert": directory / "client.pem",
        "client_key": directory / "client.key",
    }
    paths["ca"].write_bytes(material.ca_cert_pem)
    paths["client_cert"].write_bytes(material.client_cert_pem)
    paths["client_key"].write_bytes(material.client_key_pem)
    return paths


@dataclass
class FakeAegisServicer(aegis_pb2_grpc.AegisServicer):
    """Minimal, real ``PushReferenceData`` behaviour: rejects on shape, else applies.

    ``reject_symbols`` simulates Aegis's own item-by-item refusal (e.g. an unknown
    symbol not on the C03 allowlist) so client-side rejection handling can be
    tested against a real response, not a mock.
    """

    reject_symbols: set[str] = field(default_factory=set)
    fail_regime_symbols: set[str] = field(default_factory=set)
    applied: list[aegis_pb2.ReferenceSnapshot] = field(default_factory=list)
    applied_regimes: list[aegis_pb2.RegimeLabelPacket] = field(default_factory=list)
    calls: int = 0

    def PushReferenceData(self, request, context):  # noqa: N802 - grpc method name
        self.calls += 1
        rejected = []
        for snap in request.snapshots:
            if snap.symbol in self.reject_symbols:
                rejected.append(
                    aegis_pb2.ReferenceRejection(key=snap.symbol, reason="unknown_symbol")
                )
                continue
            if snap.mid_price_nanos <= 0 or snap.adv_30d_nanos <= 0:
                rejected.append(aegis_pb2.ReferenceRejection(key=snap.symbol, reason="invalid"))
                continue
            self.applied.append(snap)
        snapshot_rejections = len(rejected)
        regime_applied = False
        if request.HasField("regime"):
            self.applied_regimes.append(request.regime)
            regime_applied = True
        applied_symbol_regimes = 0
        for packet in request.symbol_regimes:
            if not packet.symbol:
                rejected.append(aegis_pb2.ReferenceRejection(key="regime:", reason="invalid"))
            elif packet.symbol in self.fail_regime_symbols:
                rejected.append(
                    aegis_pb2.ReferenceRejection(key=f"regime:{packet.symbol}", reason="stale")
                )
            else:
                self.applied_regimes.append(packet)
                applied_symbol_regimes += 1
        return aegis_pb2.PushReferenceDataResponse(
            applied_snapshots=len(request.snapshots) - snapshot_rejections,
            regime_applied=regime_applied,
            rejected=rejected,
            applied_symbol_regimes=applied_symbol_regimes,
        )


def start_fake_aegis_server(
    servicer: FakeAegisServicer, material: TlsMaterial
) -> tuple[grpc.Server, int]:
    """Starts a real, in-process mTLS gRPC server on an ephemeral loopback port."""
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    aegis_pb2_grpc.add_AegisServicer_to_server(servicer, server)
    credentials = grpc.ssl_server_credentials(
        [(material.server_key_pem, material.server_cert_pem)],
        root_certificates=material.ca_cert_pem,
        require_client_auth=True,
    )
    port = server.add_secure_port("127.0.0.1:0", credentials)
    server.start()
    return server, port
