import hashlib
import hmac
import math

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from multidrop.backends.quickshare import crypto
from multidrop.backends.quickshare.pb import securemessage_pb2 as sm_pb, ukey_pb2


def test_salts_match_protocol_doc():
    # Constants quoted in NearDrop PROTOCOL.md.
    assert crypto.D2D_SALT.hex().upper() == \
        "82AA55A0D397F88346CA1CEE8D3909B95F13FA7DEB1D4AB38376B8256DA85510"
    assert crypto.SECUREMESSAGE_SALT.hex().upper() == \
        "BF9D2A53C63616D75DB0A7165B91C1EF73E537F2427405FA23610A4BE657642E"


def _hkdf_rfc5869(ikm, salt, info, length):
    # Plain RFC 5869, as in NearDrop's fallback hkdfExtract/hkdfExpand.
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    okm, t, i = b"", b"", 0
    while len(okm) < length:
        i += 1
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:length]


def test_hkdf_matches_rfc5869():
    assert crypto.hkdf(b"k" * 32, b"UKEY2 v1 auth", b"info") == \
        _hkdf_rfc5869(b"k" * 32, b"UKEY2 v1 auth", b"info", 32)


def _pin_reference(auth: bytes) -> str:
    # Literal transcription of the Chromium loop with C semantics (fmod
    # truncates toward zero, like C++ %).
    h, mult = 0, 1
    for b in auth:
        signed = b if b < 128 else b - 256
        h = int(math.fmod(h + signed * mult, 9973))
        mult = int(math.fmod(mult * 31, 9973))
    return "%04d" % abs(h)


@pytest.mark.parametrize("auth", [
    b"\x01", b"\xff", b"\x80" * 32, bytes(range(32)), bytes(range(224, 256)),
    hashlib.sha256(b"x").digest(), hashlib.sha256(b"y").digest(),
])
def test_pin(auth):
    assert crypto.pin_from_auth_string(auth) == _pin_reference(auth)


def test_pin_small_vectors():
    assert crypto.pin_from_auth_string(b"\x01") == "0001"
    assert crypto.pin_from_auth_string(b"\xff") == "0001"      # -1 -> abs
    assert crypto.pin_from_auth_string(b"\x01\x01") == "0032"  # 1 + 1*31


def test_public_key_encoding_roundtrip():
    for _ in range(20):
        key = ec.generate_private_key(ec.SECP256R1()).public_key()
        raw = crypto.encode_public_key(key)
        msg = sm_pb.GenericPublicKey()
        msg.ParseFromString(raw)
        nums = key.public_numbers()
        # Java BigInteger form: a leading 0x00 exactly when the top bit is set.
        assert len(msg.ec_p256_public_key.x) == 33 if nums.x >> 255 else \
            len(msg.ec_p256_public_key.x) <= 32
        assert crypto.decode_public_key(raw).public_numbers() == nums


def test_decode_short_coordinates():
    # A coordinate with leading zero bytes may arrive shorter than 32 bytes.
    for _ in range(5000):
        key = ec.generate_private_key(ec.SECP256R1()).public_key()
        nums = key.public_numbers()
        if nums.x < 1 << 248:
            break
    else:
        pytest.skip("no short coordinate found")
    xb = nums.x.to_bytes(32, "big").lstrip(b"\x00")
    msg = sm_pb.GenericPublicKey(type=sm_pb.EC_P256, ec_p256_public_key=sm_pb.EcP256PublicKey(
        x=xb, y=nums.y.to_bytes(32, "big")))
    assert crypto.decode_public_key(msg.SerializeToString()).public_numbers() == nums


def _handshake():
    client, server = crypto.Ukey2Client(), crypto.Ukey2Server()
    server_init = server.handle_client_init(client.client_init)
    ckeys = client.handle_server_init(server_init)
    skeys = server.handle_client_finish(client.client_finish)
    return ckeys, skeys


def test_handshake_agrees():
    ckeys, skeys = _handshake()
    assert ckeys.auth_string == skeys.auth_string
    assert ckeys.pin == skeys.pin and len(ckeys.pin) == 4
    assert ckeys.encrypt_key == skeys.decrypt_key
    assert ckeys.decrypt_key == skeys.encrypt_key
    assert ckeys.send_hmac_key == skeys.recv_hmac_key
    assert ckeys.encrypt_key != ckeys.decrypt_key


def test_secure_channel_roundtrip_and_tamper():
    ckeys, skeys = _handshake()
    c, s = crypto.SecureChannel(ckeys), crypto.SecureChannel(skeys)
    for msg in (b"hello", b"", b"x" * 100_000):
        assert s.decrypt(c.encrypt(msg)) == msg
        assert c.decrypt(s.encrypt(msg)) == msg

    wire = c.encrypt(b"secret")
    smsg = sm_pb.SecureMessage()
    smsg.ParseFromString(wire)
    smsg.signature = bytes(32)
    with pytest.raises(crypto.SecureChannelError, match="signature"):
        s.decrypt(smsg.SerializeToString())


def test_secure_channel_rejects_replay_and_garbage():
    ckeys, skeys = _handshake()
    c, s = crypto.SecureChannel(ckeys), crypto.SecureChannel(skeys)
    first = c.encrypt(b"one")
    assert s.decrypt(first) == b"one"
    with pytest.raises(crypto.SecureChannelError, match="sequence"):
        s.decrypt(first)
    with pytest.raises(crypto.SecureChannelError):
        s.decrypt(b"\x00garbage\xff")


def test_wrong_direction_keys_fail():
    ckeys, _ = _handshake()
    c = crypto.SecureChannel(ckeys)
    with pytest.raises(crypto.SecureChannelError):
        c.decrypt(c.encrypt(b"loop"))


def test_commitment_mismatch():
    client, other = crypto.Ukey2Client(), crypto.Ukey2Client()
    server = crypto.Ukey2Server()
    server.handle_client_init(client.client_init)
    with pytest.raises(crypto.Ukey2Error, match="commitment"):
        server.handle_client_finish(other.client_finish)


def test_bad_client_init_alerts():
    server = crypto.Ukey2Server()
    ci = ukey_pb2.Ukey2ClientInit(version=2, random=bytes(32), next_protocol=crypto.NEXT_PROTOCOL)
    raw = ukey_pb2.Ukey2Message(message_type=ukey_pb2.Ukey2Message.CLIENT_INIT,
                                message_data=ci.SerializeToString()).SerializeToString()
    with pytest.raises(crypto.Ukey2Error) as e:
        server.handle_client_init(raw)
    assert e.value.alert == ukey_pb2.Ukey2Alert.BAD_VERSION
    with pytest.raises(crypto.Ukey2Error) as e:
        crypto.Ukey2Server().handle_client_init(b"\xff\xff\xff")
    assert e.value.alert == ukey_pb2.Ukey2Alert.BAD_MESSAGE
