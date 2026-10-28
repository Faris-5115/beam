"""One Quick Share TCP connection: framing, encryption, keep-alive, payloads.

Before the handshake completes, callers use read_raw/write_raw directly.
After start_secure(), a reader task decrypts incoming frames, answers
keep-alives, reassembles BYTES payloads and hands everything else to the
protocol state machine through next_event().
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass

from multidrop.core import TransferError

from . import frames
from .crypto import SecureChannel, SecureChannelError, SessionKeys
from .pb import offline_wire_formats_pb2 as ow, wire_format_pb2 as wf

log = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 10.0
HANDSHAKE_TIMEOUT = 15.0
FILE_CHUNK = 512 * 1024


class ProtocolError(TransferError):
    pass


@dataclass
class Event:
    kind: str                     # "bytes", "file", "closed", "error"
    payload_id: int = 0
    data: bytes = b""
    transfer: ow.PayloadTransferFrame | None = None
    reason: str = ""


class QSConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername")
        self.peer_address = peer[0] if peer else "?"
        self.channel: SecureChannel | None = None
        self.keys: SessionKeys | None = None
        self._lock = asyncio.Lock()
        self._events: asyncio.Queue[Event] = asyncio.Queue(maxsize=64)
        self._buffers: dict[int, bytearray] = {}
        self._tasks: list[asyncio.Task] = []
        self._closed = False

    # -- raw framing ------------------------------------------------------

    async def read_raw(self, timeout: float | None = HANDSHAKE_TIMEOUT) -> bytes:
        async def read():
            header = await self.reader.readexactly(4)
            (length,) = struct.unpack(">I", header)
            if length > frames.MAX_FRAME:
                raise ProtocolError(f"frame too large ({length} bytes)")
            return await self.reader.readexactly(length)
        try:
            return await asyncio.wait_for(read(), timeout)
        except asyncio.IncompleteReadError:
            raise ProtocolError("connection closed by peer") from None
        except asyncio.TimeoutError:
            raise ProtocolError("peer did not answer in time") from None

    async def write_raw(self, data: bytes) -> None:
        async with self._lock:
            self._write_locked(data)
            await self.writer.drain()

    def _write_locked(self, data: bytes) -> None:
        if self.channel is not None:
            data = self.channel.encrypt(data)
        self.writer.write(struct.pack(">I", len(data)) + data)

    async def read_offline_plain(self, expected_type: int) -> ow.V1Frame:
        raw = await self.read_raw()
        frame = ow.OfflineFrame()
        try:
            frame.ParseFromString(raw)
        except Exception as e:
            raise ProtocolError("malformed offline frame") from e
        if frame.v1.type != expected_type:
            raise ProtocolError(f"expected frame type {expected_type}, got {frame.v1.type}")
        return frame.v1

    # -- secure phase -----------------------------------------------------

    def start_secure(self, keys: SessionKeys) -> None:
        self.keys = keys
        self.channel = SecureChannel(keys)
        self._tasks.append(asyncio.ensure_future(self._reader_loop()))
        self._tasks.append(asyncio.ensure_future(self._keepalive_loop()))

    async def send_offline(self, frame: bytes) -> None:
        """Send an offline frame (encrypted once the channel is up)."""
        await self.write_raw(frame)

    async def send_bytes_payload(self, payload_id: int, data: bytes) -> None:
        # Like Android: the whole message, then an empty LAST_CHUNK frame.
        size = len(data)
        async with self._lock:
            self._write_locked(frames.payload_frame(
                payload_id, ow.PayloadTransferFrame.PayloadHeader.BYTES,
                size, 0, data, last=False))
            self._write_locked(frames.payload_frame(
                payload_id, ow.PayloadTransferFrame.PayloadHeader.BYTES,
                size, size, b"", last=True))
            await self.writer.drain()

    async def send_sharing(self, frame: bytes) -> None:
        await self.send_bytes_payload(frames.random_payload_id(), frame)

    async def send_file_chunk(self, payload_id: int, total: int, offset: int,
                              body: bytes, last: bool) -> None:
        await self.write_raw(frames.payload_frame(
            payload_id, ow.PayloadTransferFrame.PayloadHeader.FILE,
            total, offset, body, last))

    async def next_event(self, timeout: float | None = None) -> Event:
        try:
            return await asyncio.wait_for(self._events.get(), timeout)
        except asyncio.TimeoutError:
            raise ProtocolError("peer went silent") from None

    def pending_event(self) -> Event | None:
        try:
            return self._events.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def expect_sharing(self, frame_type: int, timeout: float | None
                             = HANDSHAKE_TIMEOUT) -> wf.V1Frame:
        """Wait for a sharing frame of the given type (sent as BYTES payload)."""
        while True:
            ev = await self.next_event(timeout)
            if ev.kind in ("closed", "error"):
                raise ProtocolError(ev.reason or "connection closed")
            if ev.kind != "bytes":
                continue
            v1 = parse_sharing(ev.data)
            if v1 is None:
                continue
            if v1.type == wf.V1Frame.CANCEL:
                raise TransferError("cancelled by the other device")
            if v1.type == frame_type:
                return v1
            log.debug("ignoring sharing frame type %s", v1.type)

    async def _put(self, ev: Event) -> None:
        await self._events.put(ev)

    async def _reader_loop(self) -> None:
        try:
            while True:
                try:
                    raw = await self.read_raw(timeout=None)
                except ProtocolError as e:
                    await self._put(Event("closed", reason=str(e)))
                    return
                try:
                    plain = self.channel.decrypt(raw)
                    frame = ow.OfflineFrame()
                    frame.ParseFromString(plain)
                except (SecureChannelError, Exception) as e:
                    await self._put(Event("error", reason=f"bad frame: {e}"))
                    return
                v1 = frame.v1
                if v1.type == ow.V1Frame.KEEP_ALIVE:
                    if not v1.keep_alive.ack:
                        await self.write_raw(frames.keep_alive(ack=True))
                elif v1.type == ow.V1Frame.DISCONNECTION:
                    await self._put(Event("closed", reason="peer disconnected"))
                    return
                elif v1.type == ow.V1Frame.PAYLOAD_TRANSFER:
                    if not await self._handle_payload(v1.payload_transfer):
                        return
        except (ConnectionError, OSError) as e:
            await self._put(Event("closed", reason=str(e)))

    async def _handle_payload(self, pt: ow.PayloadTransferFrame) -> bool:
        if pt.packet_type == ow.PayloadTransferFrame.CONTROL:
            if pt.control_message.event in (
                    ow.PayloadTransferFrame.ControlMessage.PAYLOAD_CANCELED,
                    ow.PayloadTransferFrame.ControlMessage.PAYLOAD_ERROR):
                await self._put(Event("error", reason="transfer cancelled by the other device"))
                return False
            return True
        header, chunk = pt.payload_header, pt.payload_chunk
        if header.type == ow.PayloadTransferFrame.PayloadHeader.FILE:
            await self._put(Event("file", payload_id=header.id, transfer=pt))
            return True
        if header.type != ow.PayloadTransferFrame.PayloadHeader.BYTES:
            return True
        if header.total_size > frames.MAX_FRAME:
            await self._put(Event("error", reason="bytes payload too large"))
            return False
        buf = self._buffers.setdefault(header.id, bytearray())
        if chunk.offset != len(buf) or len(buf) + len(chunk.body) > frames.MAX_FRAME:
            await self._put(Event("error", reason="bad payload chunk offset"))
            return False
        buf += chunk.body
        if chunk.flags & 1:
            del self._buffers[header.id]
            await self._put(Event("bytes", payload_id=header.id, data=bytes(buf)))
        return True

    async def _keepalive_loop(self) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                await self.write_raw(frames.keep_alive(ack=False))

    async def close(self, send_disconnect: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        for t in self._tasks:
            t.cancel()
        if send_disconnect and self.channel is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.write_raw(frames.disconnection()), 2)
        self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), 2)


def parse_sharing(data: bytes) -> wf.V1Frame | None:
    frame = wf.Frame()
    try:
        frame.ParseFromString(data)
    except Exception:
        return None
    if not frame.HasField("v1"):
        return None
    return frame.v1
