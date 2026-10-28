"""UKEY2 key exchange and the SecureMessage layer used by Quick Share.

References: NearDrop PROTOCOL.md (Unlicense), google/ukey2 README, Chromium
securemessage/d2d_crypto_ops. Only the P256_SHA512 handshake cipher and the
"AES_256_CBC-HMAC_SHA256" next protocol are used by Quick Share.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .pb import (device_to_device_messages_pb2 as d2d_pb,
                 securegcm_pb2, securemessage_pb2 as sm_pb, ukey_pb2)

NEXT_PROTOCOL = "AES_256_CBC-HMAC_SHA256"
D2D_SALT = hashlib.sha256(b"D2D").digest()
SECUREMESSAGE_SALT = hashlib.sha256(b"SecureMessage").digest()


class Ukey2Error(Exception):
    """Handshake failure; `alert` is the Ukey2Alert type to send, if any."""

    def __init__(self, message: str, alert: int | None = None):
        super().__init__(message)
        self.alert = alert


def hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt,
                info=info).derive(ikm)


# -- EC key encoding --------------------------------------------------------

def _to_signed_be(value: int) -> bytes:
    """Java BigInteger.toByteArray(): minimal two's complement, big endian."""
    length = value.bit_length() // 8 + 1
    return value.to_bytes(length, "big")


def encode_public_key(key: ec.EllipticCurvePublicKey) -> bytes:
    nums = key.public_numbers()
    msg = sm_pb.GenericPublicKey(
        type=sm_pb.EC_P256,
        ec_p256_public_key=sm_pb.EcP256PublicKey(
            x=_to_signed_be(nums.x), y=_to_signed_be(nums.y)))
    return msg.SerializeToString()


def _coord(raw: bytes) -> int:
    # Two's complement with a possible leading 0x00, or shorter than 32 bytes.
    if len(raw) > 32:
        raw = raw[-32:]
    return int.from_bytes(raw, "big")


def decode_public_key(data: bytes) -> ec.EllipticCurvePublicKey:
    try:
        msg = sm_pb.GenericPublicKey()
        msg.ParseFromString(data)
        if not msg.HasField("ec_p256_public_key"):
            raise ValueError("not an EC P-256 key")
        p = msg.ec_p256_public_key
        return ec.EllipticCurvePublicNumbers(
            _coord(p.x), _coord(p.y), ec.SECP256R1()).public_key()
    except Exception as e:
        raise Ukey2Error(f"bad public key: {e}",
                         ukey_pb2.Ukey2Alert.BAD_PUBLIC_KEY) from e


# -- key derivation ---------------------------------------------------------

def pin_from_auth_string(auth: bytes) -> str:
    """The 4-digit code both devices show (Chromium nearby_sharing_service_impl)."""
    h, mult = 0, 1
    for b in auth:
        signed = b - 256 if b > 127 else b
        # C++ % truncates toward zero; Python's % does not.
        h = _cmod(h + signed * mult, 9973)
        mult = _cmod(mult * 31, 9973)
    return f"{abs(h):04d}"


def _cmod(a: int, m: int) -> int:
    r = abs(a) % m
    return -r if a < 0 else r


@dataclass
class SessionKeys:
    auth_string: bytes
    pin: str
    encrypt_key: bytes
    decrypt_key: bytes
    send_hmac_key: bytes
    recv_hmac_key: bytes


def derive_keys(shared_secret: bytes, client_init: bytes, server_init: bytes,
                is_server: bool) -> SessionKeys:
    """client_init/server_init: the full serialized Ukey2Message bytes."""
    dhs = hashlib.sha256(shared_secret).digest()
    info = client_init + server_init
    auth = hkdf(dhs, b"UKEY2 v1 auth", info)
    next_secret = hkdf(dhs, b"UKEY2 v1 next", info)
    d2d_client = hkdf(next_secret, D2D_SALT, b"client")
    d2d_server = hkdf(next_secret, D2D_SALT, b"server")
    client_enc = hkdf(d2d_client, SECUREMESSAGE_SALT, b"ENC:2")
    client_sig = hkdf(d2d_client, SECUREMESSAGE_SALT, b"SIG:1")
    server_enc = hkdf(d2d_server, SECUREMESSAGE_SALT, b"ENC:2")
    server_sig = hkdf(d2d_server, SECUREMESSAGE_SALT, b"SIG:1")
    if is_server:
        enc, dec, ssig, rsig = server_enc, client_enc, server_sig, client_sig
    else:
        enc, dec, ssig, rsig = client_enc, server_enc, client_sig, server_sig
    return SessionKeys(auth, pin_from_auth_string(auth), enc, dec, ssig, rsig)


def _ukey2_message(msg_type: int, data: bytes) -> bytes:
    return ukey_pb2.Ukey2Message(message_type=msg_type,
                                 message_data=data).SerializeToString()


def alert_message(alert_type: int) -> bytes:
    return _ukey2_message(ukey_pb2.Ukey2Message.ALERT,
                          ukey_pb2.Ukey2Alert(type=alert_type).SerializeToString())


def _parse_ukey2(raw: bytes, expected_type: int):
    msg = ukey_pb2.Ukey2Message()
    try:
        msg.ParseFromString(raw)
    except Exception as e:
        raise Ukey2Error("cannot parse Ukey2Message",
                         ukey_pb2.Ukey2Alert.BAD_MESSAGE) from e
    if msg.message_type == ukey_pb2.Ukey2Message.ALERT:
        alert = ukey_pb2.Ukey2Alert()
        alert.ParseFromString(msg.message_data)
        raise Ukey2Error(f"peer sent UKEY2 alert {alert.type}")
    if msg.message_type != expected_type:
        raise Ukey2Error("unexpected UKEY2 message type",
                         ukey_pb2.Ukey2Alert.BAD_MESSAGE_TYPE)
    return msg.message_data


# -- handshake, server side -------------------------------------------------

class Ukey2Server:
    def __init__(self):
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._client_init: bytes | None = None
        self._server_init: bytes | None = None
        self._commitment: bytes | None = None

    def handle_client_init(self, raw: bytes) -> bytes:
        """Validate ClientInit, return the ServerInit message to send."""
        data = _parse_ukey2(raw, ukey_pb2.Ukey2Message.CLIENT_INIT)
        ci = ukey_pb2.Ukey2ClientInit()
        try:
            ci.ParseFromString(data)
        except Exception as e:
            raise Ukey2Error("bad ClientInit",
                             ukey_pb2.Ukey2Alert.BAD_MESSAGE_DATA) from e
        if ci.version != 1:
            raise Ukey2Error("bad version", ukey_pb2.Ukey2Alert.BAD_VERSION)
        if len(ci.random) != 32:
            raise Ukey2Error("bad random", ukey_pb2.Ukey2Alert.BAD_RANDOM)
        for c in ci.cipher_commitments:
            if c.handshake_cipher == ukey_pb2.P256_SHA512:
                self._commitment = c.commitment
                break
        else:
            raise Ukey2Error("no P256_SHA512 commitment",
                             ukey_pb2.Ukey2Alert.BAD_HANDSHAKE_CIPHER)
        if ci.next_protocol != NEXT_PROTOCOL:
            raise Ukey2Error("bad next protocol",
                             ukey_pb2.Ukey2Alert.BAD_NEXT_PROTOCOL)
        self._client_init = raw
        si = ukey_pb2.Ukey2ServerInit(
            version=1, random=os.urandom(32),
            handshake_cipher=ukey_pb2.P256_SHA512,
            public_key=encode_public_key(self._key.public_key()))
        self._server_init = _ukey2_message(ukey_pb2.Ukey2Message.SERVER_INIT,
                                           si.SerializeToString())
        return self._server_init

    def handle_client_finish(self, raw: bytes) -> SessionKeys:
        if self._server_init is None:
            raise Ukey2Error("ClientFinish before ClientInit")
        if not hmac.compare_digest(hashlib.sha512(raw).digest(),
                                   self._commitment or b""):
            raise Ukey2Error("ClientFinish does not match commitment")
        data = _parse_ukey2(raw, ukey_pb2.Ukey2Message.CLIENT_FINISH)
        cf = ukey_pb2.Ukey2ClientFinished()
        cf.ParseFromString(data)
        peer = decode_public_key(cf.public_key)
        secret = self._key.exchange(ec.ECDH(), peer)
        return derive_keys(secret, self._client_init, self._server_init,
                           is_server=True)


# -- handshake, client side -------------------------------------------------

class Ukey2Client:
    def __init__(self):
        self._key = ec.generate_private_key(ec.SECP256R1())
        cf = ukey_pb2.Ukey2ClientFinished(
            public_key=encode_public_key(self._key.public_key()))
        self.client_finish = _ukey2_message(ukey_pb2.Ukey2Message.CLIENT_FINISH,
                                            cf.SerializeToString())
        ci = ukey_pb2.Ukey2ClientInit(
            version=1, random=os.urandom(32), next_protocol=NEXT_PROTOCOL,
            cipher_commitments=[ukey_pb2.Ukey2ClientInit.CipherCommitment(
                handshake_cipher=ukey_pb2.P256_SHA512,
                commitment=hashlib.sha512(self.client_finish).digest())])
        self.client_init = _ukey2_message(ukey_pb2.Ukey2Message.CLIENT_INIT,
                                          ci.SerializeToString())

    def handle_server_init(self, raw: bytes) -> SessionKeys:
        data = _parse_ukey2(raw, ukey_pb2.Ukey2Message.SERVER_INIT)
        si = ukey_pb2.Ukey2ServerInit()
        try:
            si.ParseFromString(data)
        except Exception as e:
            raise Ukey2Error("bad ServerInit",
                             ukey_pb2.Ukey2Alert.BAD_MESSAGE_DATA) from e
        if si.version != 1:
            raise Ukey2Error("bad version", ukey_pb2.Ukey2Alert.BAD_VERSION)
        if len(si.random) != 32:
            raise Ukey2Error("bad random", ukey_pb2.Ukey2Alert.BAD_RANDOM)
        if si.handshake_cipher != ukey_pb2.P256_SHA512:
            raise Ukey2Error("bad cipher", ukey_pb2.Ukey2Alert.BAD_HANDSHAKE_CIPHER)
        peer = decode_public_key(si.public_key)
        secret = self._key.exchange(ec.ECDH(), peer)
        return derive_keys(secret, self.client_init, raw, is_server=False)


# -- SecureMessage ------------------------------------------------------------

class SecureChannelError(Exception):
    pass


class SecureChannel:
    """Encrypts/decrypts offline frames after the handshake."""

    def __init__(self, keys: SessionKeys):
        self.keys = keys
        self._send_seq = 0
        self._recv_seq = 0
        self._metadata = securegcm_pb2.GcmMetadata(
            type=securegcm_pb2.DEVICE_TO_DEVICE_MESSAGE,
            version=1).SerializeToString()

    def encrypt(self, payload: bytes) -> bytes:
        self._send_seq += 1
        plain = d2d_pb.DeviceToDeviceMessage(
            sequence_number=self._send_seq, message=payload).SerializeToString()
        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(self.keys.encrypt_key), modes.CBC(iv)).encryptor()
        body = enc.update(padded) + enc.finalize()
        hb = sm_pb.HeaderAndBody(
            header=sm_pb.Header(signature_scheme=sm_pb.HMAC_SHA256,
                                encryption_scheme=sm_pb.AES_256_CBC,
                                iv=iv, public_metadata=self._metadata),
            body=body).SerializeToString()
        sig = hmac.new(self.keys.send_hmac_key, hb, hashlib.sha256).digest()
        return sm_pb.SecureMessage(header_and_body=hb,
                                   signature=sig).SerializeToString()

    def decrypt(self, raw: bytes) -> bytes:
        try:
            smsg = sm_pb.SecureMessage()
            smsg.ParseFromString(raw)
        except Exception as e:
            raise SecureChannelError("not a SecureMessage") from e
        expected = hmac.new(self.keys.recv_hmac_key, smsg.header_and_body,
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, smsg.signature):
            raise SecureChannelError("bad signature")
        try:
            hb = sm_pb.HeaderAndBody()
            hb.ParseFromString(smsg.header_and_body)
            if hb.header.encryption_scheme != sm_pb.AES_256_CBC:
                raise SecureChannelError("unexpected encryption scheme")
            dec = Cipher(algorithms.AES(self.keys.decrypt_key),
                         modes.CBC(hb.header.iv)).decryptor()
            padded = dec.update(hb.body) + dec.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            plain = unpadder.update(padded) + unpadder.finalize()
            msg = d2d_pb.DeviceToDeviceMessage()
            msg.ParseFromString(plain)
        except SecureChannelError:
            raise
        except Exception as e:
            raise SecureChannelError(f"cannot decrypt: {e}") from e
        self._recv_seq += 1
        if msg.sequence_number != self._recv_seq:
            raise SecureChannelError(
                f"sequence {msg.sequence_number}, expected {self._recv_seq}")
        return msg.message
