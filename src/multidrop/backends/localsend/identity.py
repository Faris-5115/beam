"""Device identity: a self-signed TLS certificate, as the official app uses.

Matches LocalSend's own (packages/core/src/crypto/cert.rs): RSA-2048,
CN=LocalSend User, and the fingerprint is the SHA-256 of the certificate's DER
encoding as uppercase hex.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def fingerprint_from_der(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest().upper()


@dataclass
class Identity:
    cert_path: Path
    key_path: Path
    fingerprint: str

    def server_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx

    def client_ssl_context(self) -> ssl.SSLContext:
        """Presents our certificate to servers; they are pinned separately.

        LocalSend >= 1.18 (Rust core) makes the client certificate mandatory
        unless its browser share is on (client_cert_verifier.rs).
        """
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.load_cert_chain(self.cert_path, self.key_path)
        return ctx

    @classmethod
    def load_or_create(cls, directory: Path,
                       common_name: str = "LocalSend User") -> "Identity":
        cert_path = directory / "cert.pem"
        key_path = directory / "key.pem"
        if not (cert_path.exists() and key_path.exists()):
            _generate(cert_path, key_path, common_name)
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        der = cert.public_bytes(serialization.Encoding.DER)
        return cls(cert_path, key_path, fingerprint_from_der(der))


def _generate(cert_path: Path, key_path: Path, common_name: str) -> None:
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * 20))
        .sign(key, hashes.SHA256())
    )
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    # The key is written owner-only before any content goes in.
    with open(key_path, "wb", opener=lambda p, f: os.open(p, f, 0o600)) as f:
        f.write(key_bytes)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
