"""Quick Share wire details: endpoint info, mDNS names, frame builders.

Formats follow NearDrop PROTOCOL.md ("How devices find each other").
"""

from __future__ import annotations

import base64
import os
import random
import string
from dataclasses import dataclass

from .pb import offline_wire_formats_pb2 as ow, wire_format_pb2 as wf

SERVICE_TYPE = "_FC9F5ED42C8A._tcp.local."
SERVICE_ID_HASH = bytes([0xFC, 0x9F, 0x5E])
# multidrop listens here by default (config quickshare.port); Android picks
# random ports, so for phones the port comes from mDNS.
DEFAULT_PORT = 53318
PCP = 0x23
MAX_FRAME = 5 * 1024 * 1024

# Device types in the endpoint info byte (bits 3..1).
DEVICE_UNKNOWN, DEVICE_PHONE, DEVICE_TABLET, DEVICE_LAPTOP = 0, 1, 2, 3
DEVICE_TYPE_NAMES = {0: "unknown", 1: "phone", 2: "tablet", 3: "laptop",
                     4: "car", 5: "foldable", 6: "xr"}


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_endpoint_id() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(4))


def mdns_instance_name(endpoint_id: str) -> str:
    raw = bytes([PCP]) + endpoint_id.encode("ascii") + SERVICE_ID_HASH + b"\0\0"
    return b64url(raw)


def parse_mdns_instance_name(name: str) -> str | None:
    """Endpoint id from a service instance name, or None if not Quick Share."""
    try:
        raw = b64url_decode(name)
    except (ValueError, UnicodeEncodeError):
        return None
    if len(raw) < 10 or raw[0] != PCP or raw[5:8] != SERVICE_ID_HASH:
        return None
    try:
        return raw[1:5].decode("ascii")
    except UnicodeDecodeError:
        return None


TLV_QR_CODE = 1
TLV_VENDOR = 2


@dataclass
class EndpointInfo:
    name: str | None
    device_type: int = DEVICE_LAPTOP
    visible: bool = True
    qr_data: bytes | None = None     # TLV 1, see qr.py

    def serialize(self) -> bytes:
        name = (self.name or "").encode()[:255]
        first = (self.device_type & 7) << 1 | (0 if self.visible else 0x10)
        out = bytes([first]) + os.urandom(16)
        if self.visible:
            out += bytes([len(name)]) + name
        if self.qr_data is not None:
            out += bytes([TLV_QR_CODE, len(self.qr_data)]) + self.qr_data
        return out

    @classmethod
    def parse(cls, data: bytes) -> "EndpointInfo":
        if len(data) < 17:
            raise ValueError("endpoint info too short")
        visible = not data[0] & 0x10
        device_type = (data[0] >> 1) & 7
        name = None
        offset = 17
        if visible:
            if len(data) < 18 or len(data) < 18 + data[17]:
                raise ValueError("endpoint info too short for name")
            name = data[18:18 + data[17]].decode("utf-8", "replace")
            offset = 18 + data[17]
        qr = None
        while len(data) - offset >= 2:          # optional TLV records
            tlv_type, length = data[offset], data[offset + 1]
            value = data[offset + 2:offset + 2 + length]
            if len(value) < length:
                break
            if tlv_type == TLV_QR_CODE:
                qr = value
            offset += 2 + length
        return cls(name, device_type, visible, qr)


# -- offline frames ---------------------------------------------------------

def offline(v1: ow.V1Frame) -> ow.OfflineFrame:
    return ow.OfflineFrame(version=ow.OfflineFrame.V1, v1=v1)


def connection_request(endpoint_id: str, info: EndpointInfo) -> bytes:
    return offline(ow.V1Frame(
        type=ow.V1Frame.CONNECTION_REQUEST,
        connection_request=ow.ConnectionRequestFrame(
            endpoint_id=endpoint_id, endpoint_name=info.name or "",
            endpoint_info=info.serialize(),
            mediums=[ow.ConnectionRequestFrame.WIFI_LAN]))).SerializeToString()


def connection_response(accept: bool = True) -> bytes:
    resp = ow.ConnectionResponseFrame(
        status=0 if accept else 1,
        response=(ow.ConnectionResponseFrame.ACCEPT if accept
                  else ow.ConnectionResponseFrame.REJECT),
        os_info=ow.OsInfo(type=ow.OsInfo.LINUX))
    return offline(ow.V1Frame(type=ow.V1Frame.CONNECTION_RESPONSE,
                              connection_response=resp)).SerializeToString()


def keep_alive(ack: bool) -> bytes:
    return offline(ow.V1Frame(type=ow.V1Frame.KEEP_ALIVE,
                              keep_alive=ow.KeepAliveFrame(ack=ack))).SerializeToString()


def disconnection() -> bytes:
    return offline(ow.V1Frame(type=ow.V1Frame.DISCONNECTION,
                              disconnection=ow.DisconnectionFrame())).SerializeToString()


def payload_frame(payload_id: int, ptype: int, total: int, offset: int,
                  body: bytes, last: bool) -> bytes:
    pt = ow.PayloadTransferFrame(
        packet_type=ow.PayloadTransferFrame.DATA,
        payload_header=ow.PayloadTransferFrame.PayloadHeader(
            id=payload_id, type=ptype, total_size=total, is_sensitive=False),
        payload_chunk=ow.PayloadTransferFrame.PayloadChunk(
            offset=offset, flags=1 if last else 0, body=body))
    return offline(ow.V1Frame(type=ow.V1Frame.PAYLOAD_TRANSFER,
                              payload_transfer=pt)).SerializeToString()


# -- sharing frames ---------------------------------------------------------

def sharing(v1: wf.V1Frame) -> bytes:
    return wf.Frame(version=wf.Frame.V1, v1=v1).SerializeToString()


def paired_key_encryption(qr_signature: bytes | None = None) -> bytes:
    # Contents only matter for contact-based visibility, which needs Google
    # servers; random bytes are accepted (NearDrop, rquickshare). With a QR
    # code, qr_code_handshake_data proves we showed it (see qr.py).
    frame = wf.PairedKeyEncryptionFrame(
        secret_id_hash=os.urandom(6), signed_data=os.urandom(72))
    if qr_signature is not None:
        frame.qr_code_handshake_data = qr_signature
    return sharing(wf.V1Frame(type=wf.V1Frame.PAIRED_KEY_ENCRYPTION,
                              paired_key_encryption=frame))


def paired_key_result() -> bytes:
    return sharing(wf.V1Frame(
        type=wf.V1Frame.PAIRED_KEY_RESULT,
        paired_key_result=wf.PairedKeyResultFrame(
            status=wf.PairedKeyResultFrame.UNABLE)))


def response(status: int) -> bytes:
    return sharing(wf.V1Frame(
        type=wf.V1Frame.RESPONSE,
        connection_response=wf.ConnectionResponseFrame(status=status)))


def cancel() -> bytes:
    return sharing(wf.V1Frame(type=wf.V1Frame.CANCEL))


def random_payload_id() -> int:
    return int.from_bytes(os.urandom(8), "big", signed=True)


def file_type_for(mime: str, name: str) -> int:
    if mime.startswith("image/"):
        return wf.FileMetadata.IMAGE
    if mime.startswith("video/"):
        return wf.FileMetadata.VIDEO
    if mime.startswith("audio/"):
        return wf.FileMetadata.AUDIO
    if name.lower().endswith(".apk"):
        return wf.FileMetadata.ANDROID_APP
    return wf.FileMetadata.UNKNOWN
