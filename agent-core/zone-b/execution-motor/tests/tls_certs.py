"""Ephemeral CA/server/client certificates for TLS integration tests (no files on disk are
required by the tests themselves; ``server_config.TlsPaths`` still wants file paths, so
``write_pem`` writes into a pytest ``tmp_path``). Nothing here ships in the runtime package.
"""

from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.x509.oid import NameOID

_ONE_DAY = datetime.timedelta(days=1)


@dataclass(frozen=True)
class PemCert:
    cert_pem: bytes
    key_pem: bytes


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _key() -> EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def make_ca(cn: str = "test-ca") -> tuple[PemCert, x509.Certificate, EllipticCurvePrivateKey]:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _ONE_DAY)
        .not_valid_after(now + _ONE_DAY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    pem = PemCert(
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return pem, cert, key


def make_leaf(
    cn: str,
    ca_cert: x509.Certificate,
    ca_key: EllipticCurvePrivateKey,
    *,
    server: bool,
    dns_names: tuple[str, ...] = ("localhost",),
) -> PemCert:
    key = _key()
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _ONE_DAY)
        .not_valid_after(now + _ONE_DAY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if server:
        names: list[x509.GeneralName] = [x509.DNSName(n) for n in dns_names]
        names.append(x509.IPAddress(_ip("127.0.0.1")))
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    cert = builder.sign(ca_key, hashes.SHA256())
    return PemCert(
        cert_pem=cert.public_bytes(serialization.Encoding.PEM),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    return ipaddress.ip_address(text)


@dataclass(frozen=True)
class PkiBundle:
    ca: PemCert
    server: PemCert
    client: PemCert


def build_pki() -> PkiBundle:
    ca_pem, ca_cert, ca_key = make_ca()
    server = make_leaf("motor-server", ca_cert, ca_key, server=True)
    client = make_leaf("motor-client", ca_cert, ca_key, server=False)
    return PkiBundle(ca=ca_pem, server=server, client=client)


def write_pem(directory: Path, name: str, data: bytes) -> Path:
    path = directory / name
    path.write_bytes(data)
    return path
