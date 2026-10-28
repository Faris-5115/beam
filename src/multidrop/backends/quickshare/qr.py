"""Quick Share QR code handshake (NearDrop PROTOCOL.md, "QR codes").

The sender shows https://quickshare.google/qrcode#key=KEY. KEY is a P-256
public key: two version bytes 00 00, then the compressed point (02/03 + X).
NearDrop always writes 02; with the real parity byte the receiver can verify
our signature for either Y. Both sides derive from KEY:

  advertising token = HKDF-SHA256(KEY, salt="", "advertisingContext", 16)
  name key          = HKDF-SHA256(KEY, salt="", "encryptionKey", 16)

The phone that scanned the code puts TLV type 1 into its mDNS endpoint info:
the token itself (visible) or its name AES-GCM-encrypted with the name key and
the token as associated data (hidden). The sender connects to that device and
signs the UKEY2 auth string with the QR private key (ECDSA P-256/SHA-256,
r||s), which lets the phone skip its accept prompt.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature, encode_dss_signature)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import hkdf
from .frames import EndpointInfo, b64url, b64url_decode

URL_PREFIX = "https://quickshare.google/qrcode#key="
KEY_VERSION = b"\x00\x00"


def _derive(key_data: bytes) -> tuple[bytes, bytes]:
    return (hkdf(key_data, b"", b"advertisingContext", 16),
            hkdf(key_data, b"", b"encryptionKey", 16))


class QrSender:
    """The side that shows the QR code (multidrop sending to a phone)."""

    def __init__(self):
        self._key = ec.generate_private_key(ec.SECP256R1())
        point = self._key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
        self.key_data = KEY_VERSION + point
        self.advertising_token, self.name_key = _derive(self.key_data)

    @property
    def url(self) -> str:
        return URL_PREFIX + b64url(self.key_data)

    def match(self, info: EndpointInfo) -> str | None:
        """Name of the device if its endpoint info answers our QR code."""
        qr = info.qr_data
        if qr is None:
            return None
        if qr == self.advertising_token:
            return info.name or "QR device"
        if len(qr) > 28:
            try:
                name = AESGCM(self.name_key).decrypt(qr[:12], qr[12:], self.advertising_token)
                return name.decode("utf-8", "replace")
            except InvalidTag:
                return None
        return None

    def sign(self, auth_string: bytes) -> bytes:
        der = self._key.sign(auth_string, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


class QrScan:
    """The side that scanned the code (a phone; used for loopback tests)."""

    def __init__(self, url: str):
        if not url.startswith(URL_PREFIX):
            raise ValueError("not a Quick Share QR URL")
        self.key_data = b64url_decode(url[len(URL_PREFIX):])
        if len(self.key_data) != 35 or self.key_data[:2] != KEY_VERSION:
            raise ValueError("unexpected QR key format")
        self.public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), self.key_data[2:])
        self.advertising_token, self.name_key = _derive(self.key_data)

    def tlv_payload(self, name: str | None) -> bytes:
        """Value of TLV type 1: the token, or the name encrypted for hidden devices."""
        if name is None:
            return self.advertising_token
        iv = os.urandom(12)
        return iv + AESGCM(self.name_key).encrypt(iv, name.encode(), self.advertising_token)

    def verify(self, auth_string: bytes, signature: bytes) -> bool:
        if len(signature) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(signature[:32], "big"),
                                   int.from_bytes(signature[32:], "big"))
        try:
            self.public_key.verify(der, auth_string, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False


def terminal_qr(url: str) -> str:
    """The QR code as text for a terminal (half-block characters)."""
    import io
    import segno
    out = io.StringIO()
    segno.make(url, error="m").terminal(out=out, compact=True, border=2)
    return out.getvalue()


def svg_qr(url: str) -> str:
    """The QR code as an inline <svg> element (for the web UI)."""
    import io
    import segno
    out = io.BytesIO()
    # With the namespace, so the browser's XML parser renders it as SVG.
    segno.make(url, error="m").save(out, kind="svg", xmldecl=False, svgns=True,
                                    nl=False, scale=6, border=2)
    return out.getvalue().decode()
