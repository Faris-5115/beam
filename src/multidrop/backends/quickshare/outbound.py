"""Sending side (the Quick Share "client")."""

from __future__ import annotations

import asyncio
import contextlib
import mimetypes
from pathlib import Path

from multidrop.core import EventHandler, TransferError

from . import frames
from .connection import FILE_CHUNK, ProtocolError, QSConnection, parse_sharing
from .crypto import Ukey2Client, Ukey2Error, alert_message
from .pb import offline_wire_formats_pb2 as ow, wire_format_pb2 as wf

CONNECT_TIMEOUT = 10.0
CONSENT_TIMEOUT = 120.0
RespStatus = wf.ConnectionResponseFrame

_REFUSALS = {
    RespStatus.REJECT: "rejected by receiver",
    RespStatus.NOT_ENOUGH_SPACE: "receiver does not have enough space",
    RespStatus.UNSUPPORTED_ATTACHMENT_TYPE: "receiver does not support this content",
    RespStatus.TIMED_OUT: "receiver did not answer in time",
}


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://")) and " " not in text.strip()


async def send(host: str, port: int, *, our_name: str,
               entries: list[tuple[Path, str]] | None = None, text: str | None = None,
               on_event: EventHandler, target_name: str = "", qr=None) -> None:
    """Send files (as (path, name) pairs) or one text/URL to a receiver."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError) as e:
        raise TransferError(f"cannot connect to {host}:{port}: {e}") from None
    conn = QSConnection(reader, writer)
    try:
        await _run(conn, our_name, entries or [], text, on_event,
                   target_name or f"{host}:{port}", qr)
    except (ConnectionError, OSError) as e:
        raise TransferError(f"connection lost: {e}") from None
    finally:
        await conn.close()


async def _run(conn: QSConnection, our_name: str, entries, text, on_event, target, qr=None):
    await conn.write_raw(frames.connection_request(
        frames.new_endpoint_id(), frames.EndpointInfo(our_name, frames.DEVICE_LAPTOP)))
    ukey = Ukey2Client()
    await conn.write_raw(ukey.client_init)
    try:
        keys = ukey.handle_server_init(await conn.read_raw())
    except Ukey2Error as e:
        if e.alert is not None:
            await conn.write_raw(alert_message(e.alert))
        raise ProtocolError(f"key exchange failed: {e}") from e
    await conn.write_raw(ukey.client_finish)
    await conn.write_raw(frames.connection_response())
    resp = await conn.read_offline_plain(ow.V1Frame.CONNECTION_RESPONSE)
    if resp.connection_response.response != ow.ConnectionResponseFrame.ACCEPT:
        raise TransferError("receiver refused the connection")
    conn.start_secure(keys)
    on_event("pin", {"pin": keys.pin, "to": target})

    signature = qr.sign(keys.auth_string) if qr is not None else None
    await conn.send_sharing(frames.paired_key_encryption(signature))
    await conn.expect_sharing(wf.V1Frame.PAIRED_KEY_ENCRYPTION)
    await conn.send_sharing(frames.paired_key_result())
    await conn.expect_sharing(wf.V1Frame.PAIRED_KEY_RESULT)

    intro = wf.IntroductionFrame()
    files = []
    text_pid = None
    if text is not None:
        text_pid = frames.random_payload_id()
        data = text.encode()
        url = _is_url(text)
        intro.text_metadata.append(wf.TextMetadata(
            text_title=text.split("/")[2] if url else text[:40],
            type=wf.TextMetadata.URL if url else wf.TextMetadata.TEXT,
            payload_id=text_pid, size=len(data), id=frames.random_payload_id()))
    for path, name in entries:
        size = path.stat().st_size
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        pid = frames.random_payload_id()
        p = Path(name)
        fm = wf.FileMetadata(name=p.name, type=frames.file_type_for(mime, name),
                             payload_id=pid, size=size, mime_type=mime,
                             id=frames.random_payload_id())
        if len(p.parts) > 1:
            fm.parent_folder = str(p.parent)
        intro.file_metadata.append(fm)
        files.append((path, name, pid, size))
    await conn.send_sharing(frames.sharing(wf.V1Frame(
        type=wf.V1Frame.INTRODUCTION, introduction=intro)))
    on_event("waiting", {"to": target})

    answer = await conn.expect_sharing(wf.V1Frame.RESPONSE, timeout=CONSENT_TIMEOUT)
    status = answer.connection_response.status
    if status != RespStatus.ACCEPT:
        raise TransferError(_REFUSALS.get(status, f"refused (status {status})"))

    if text_pid is not None:
        await conn.send_bytes_payload(text_pid, text.encode())
    for path, name, pid, size in files:
        await _send_file(conn, path, name, pid, size, on_event)
        on_event("file_sent", {"file": name})

    # The receiver disconnects once it has everything; that is our receipt.
    with contextlib.suppress(ProtocolError):
        while True:
            ev = await conn.next_event(timeout=30)
            if ev.kind == "closed":
                break
            if ev.kind == "error":
                raise TransferError(ev.reason)
            if ev.kind == "bytes":
                v1 = parse_sharing(ev.data)
                if v1 is not None and v1.type == wf.V1Frame.CANCEL:
                    raise TransferError("cancelled by receiver")
    on_event("done", {"to": target, "files": len(files) + (text_pid is not None)})


async def _send_file(conn: QSConnection, path: Path, name: str, pid: int,
                     size: int, on_event: EventHandler) -> None:
    offset = 0
    with open(path, "rb") as f:
        while offset < size:
            body = await asyncio.to_thread(f.read, min(FILE_CHUNK, size - offset))
            if not body:
                raise TransferError(f"{name} shrank while sending")
            await conn.send_file_chunk(pid, size, offset, body, last=False)
            offset += len(body)
            on_event("progress", {"file": name, "done": offset, "size": size})
            ev = conn.pending_event()
            if ev is not None and ev.kind in ("closed", "error"):
                raise TransferError(f"receiver stopped: {ev.reason}")
            if ev is not None and ev.kind == "bytes":
                v1 = parse_sharing(ev.data)
                if v1 is not None and v1.type == wf.V1Frame.CANCEL:
                    raise TransferError("cancelled by receiver")
    await conn.send_file_chunk(pid, size, offset, b"", last=True)
