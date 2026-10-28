"""Receiving side (the Quick Share "server")."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path, PurePosixPath

from multidrop.core import (EventHandler, IncomingFileWriter, IncomingOffer,
                            OfferFile, OfferHandler, TransferError)

from . import frames
from .connection import ProtocolError, QSConnection, parse_sharing
from .crypto import Ukey2Error, Ukey2Server, alert_message
from .pb import offline_wire_formats_pb2 as ow, wire_format_pb2 as wf

log = logging.getLogger(__name__)

IDLE_TIMEOUT = 60.0
CONSENT_TIMEOUT = 120.0
RespStatus = wf.ConnectionResponseFrame


async def _consent(conn: QSConnection, on_offer: OfferHandler,
                   offer: IncomingOffer) -> set[str]:
    """Ask the user, but notice if the sender gives up meanwhile."""
    decision = asyncio.ensure_future(on_offer(offer))
    try:
        while not decision.done():
            watcher = asyncio.ensure_future(conn.next_event())
            done, _ = await asyncio.wait({decision, watcher}, timeout=CONSENT_TIMEOUT,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                watcher.cancel()
                raise TransferError("no decision in time")
            if watcher in done:
                ev = watcher.result()
                if ev.kind in ("closed", "error"):
                    raise TransferError(f"sender gave up: {ev.reason}")
                if ev.kind == "bytes":
                    v1 = parse_sharing(ev.data)
                    if v1 is not None and v1.type == wf.V1Frame.CANCEL:
                        raise TransferError("cancelled by sender")
            else:
                watcher.cancel()
        return decision.result()
    finally:
        decision.cancel()


def _build_offer(intro: wf.IntroductionFrame, sender: str, address: str
                 ) -> tuple[IncomingOffer, dict[int, int]]:
    """Returns the offer and the text payload ids mapped to their text type."""
    files = []
    texts: dict[int, int] = {}
    for fm in intro.file_metadata:
        name = fm.name
        if fm.parent_folder:
            name = str(PurePosixPath(fm.parent_folder) / fm.name)
        files.append(OfferFile(id=str(fm.payload_id), name=name, size=max(fm.size, 0),
                               mime=fm.mime_type or None))
    for tm in intro.text_metadata:
        texts[tm.payload_id] = tm.type
        files.append(OfferFile(id=str(tm.payload_id), name=tm.text_title or "text",
                               size=max(tm.size, 0), mime="text/plain",
                               text=tm.text_title or ""))
    return IncomingOffer("quickshare", sender, address, files), texts


async def handle_inbound(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                         download_dir: Path, on_offer: OfferHandler | None,
                         on_event: EventHandler, qr_scan=None) -> None:
    """qr_scan: a scanned QR code (qr.QrScan); a sender proving it showed
    that code is accepted without asking, as Android does."""
    conn = QSConnection(reader, writer)
    writers: dict[int, IncomingFileWriter] = {}
    sender = conn.peer_address
    accepted_transfer = False
    try:
        # 1. Connection request, plaintext.
        req = await conn.read_offline_plain(ow.V1Frame.CONNECTION_REQUEST)
        try:
            info = frames.EndpointInfo.parse(req.connection_request.endpoint_info)
            sender = info.name or req.connection_request.endpoint_name or sender
        except ValueError:
            sender = req.connection_request.endpoint_name or sender

        # 2. UKEY2.
        ukey = Ukey2Server()
        try:
            await conn.write_raw(ukey.handle_client_init(await conn.read_raw()))
            keys = ukey.handle_client_finish(await conn.read_raw())
        except Ukey2Error as e:
            if e.alert is not None:
                await conn.write_raw(alert_message(e.alert))
            raise ProtocolError(f"key exchange failed: {e}") from e

        # 3. Plaintext connection responses, then everything is encrypted.
        resp = await conn.read_offline_plain(ow.V1Frame.CONNECTION_RESPONSE)
        if resp.connection_response.response == ow.ConnectionResponseFrame.REJECT:
            raise ProtocolError("sender aborted the connection")
        await conn.write_raw(frames.connection_response())
        conn.start_secure(keys)
        on_event("pin", {"pin": keys.pin, "from": sender})

        # 4. Paired key frames (meaningless without Google accounts).
        await conn.send_sharing(frames.paired_key_encryption())
        pke = (await conn.expect_sharing(wf.V1Frame.PAIRED_KEY_ENCRYPTION)).paired_key_encryption
        qr_verified = (qr_scan is not None and qr_scan.verify(
            keys.auth_string, pke.qr_code_handshake_data))
        if qr_verified:
            on_event("qr_verified", {"from": sender})
        await conn.send_sharing(frames.paired_key_result())
        await conn.expect_sharing(wf.V1Frame.PAIRED_KEY_RESULT)

        # 5. Introduction and consent.
        intro = (await conn.expect_sharing(wf.V1Frame.INTRODUCTION)).introduction
        offer, texts = _build_offer(intro, sender, conn.peer_address)
        if not offer.files:
            await conn.send_sharing(frames.response(RespStatus.UNSUPPORTED_ATTACHMENT_TYPE))
            on_event("rejected", {"from": sender, "reason": "unsupported attachment"})
            return
        if offer.total_size > shutil.disk_usage(_existing_parent(download_dir)).free:
            await conn.send_sharing(frames.response(RespStatus.NOT_ENOUGH_SPACE))
            on_event("rejected", {"from": sender, "reason": "not enough space"})
            return
        if qr_verified:
            accepted = {f.id for f in offer.files}
        else:
            accepted = await _consent(conn, on_offer, offer) if on_offer else set()
        if not accepted:
            await conn.send_sharing(frames.response(RespStatus.REJECT))
            on_event("rejected", {"from": sender})
            return

        download_dir.mkdir(parents=True, exist_ok=True)
        files = {int(f.id): f for f in offer.files if int(f.id) not in texts}
        for pid, f in files.items():
            if f.id in accepted:
                writers[pid] = IncomingFileWriter(download_dir, f)
        await conn.send_sharing(frames.response(RespStatus.ACCEPT))
        accepted_transfer = True
        on_event("session_started", {"from": sender, "files": len(offer.files),
                                     "size": offer.total_size})

        # 6. Payloads.
        pending = set(files) | set(texts)
        received = {pid: 0 for pid in files}
        while pending:
            ev = await conn.next_event(IDLE_TIMEOUT)
            if ev.kind == "closed" and all(
                    pid in files and files[pid].size == 0 for pid in pending):
                # Some senders (rquickshare) send no chunk at all for empty
                # files and just disconnect.
                for pid in list(pending):
                    _finish_file(writers.pop(pid, None), files[pid], sender, on_event)
                pending.clear()
                break
            if ev.kind in ("closed", "error"):
                raise TransferError(f"transfer incomplete: {ev.reason}")
            pid = ev.payload_id
            if ev.kind == "bytes":
                if pid in texts and pid in pending:
                    pending.discard(pid)
                    text = ev.data.decode("utf-8", "replace")
                    on_event("message", {"from": sender, "text": text,
                                         "kind": wf.TextMetadata.Type.Name(texts[pid]).lower()})
                elif pid in files and pid in pending:
                    # Small files may arrive as a single BYTES payload.
                    _write_chunk(writers.get(pid), files[pid], received, pid, 0, ev.data)
                    _finish_file(writers.pop(pid, None), files[pid], sender, on_event)
                    pending.discard(pid)
                else:
                    v1 = parse_sharing(ev.data)
                    if v1 is not None and v1.type == wf.V1Frame.CANCEL:
                        raise TransferError("cancelled by sender")
                continue
            if ev.kind != "file":
                continue
            if pid not in files or pid not in pending:
                raise ProtocolError(f"unexpected file payload {pid}")
            chunk = ev.transfer.payload_chunk
            _write_chunk(writers.get(pid), files[pid], received, pid,
                         chunk.offset, chunk.body)
            if chunk.body:
                on_event("progress", {"file": files[pid].name, "done": received[pid],
                                      "size": files[pid].size})
            if chunk.flags & 1:
                _finish_file(writers.pop(pid, None), files[pid], sender, on_event)
                pending.discard(pid)
        on_event("session_done", {"from": sender})
    except TransferError as e:
        on_event("failed", {"from": sender, "error": str(e)})
        if accepted_transfer:
            # Tell the sender, otherwise our disconnect looks like success.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.send_sharing(frames.cancel()), 2)
    except (ConnectionError, OSError) as e:
        on_event("failed", {"from": sender, "error": str(e)})
    finally:
        for w in writers.values():
            w.abort()
        await conn.close()


def _existing_parent(path: Path) -> Path:
    while not path.exists():
        path = path.parent
    return path


def _write_chunk(writer: IncomingFileWriter | None, f: OfferFile,
                 received: dict[int, int], pid: int, offset: int, body: bytes) -> None:
    if offset != received[pid]:
        raise ProtocolError(f"bad offset {offset} for {f.name}, expected {received[pid]}")
    if received[pid] + len(body) > f.size:
        raise ProtocolError(f"{f.name}: more data than announced")
    received[pid] += len(body)
    if writer is not None:
        writer.write(body)


def _finish_file(writer: IncomingFileWriter | None, f: OfferFile, sender: str,
                 on_event: EventHandler) -> None:
    if writer is None:
        return  # not accepted, data discarded
    path = writer.finish()
    on_event("file_received", {"file": f.name, "path": str(path), "from": sender})
