"""AirDrop receiver: HTTPS POST /Discover, /Ask, /Upload (port 8771 on awdl0).

Follows opendrop and omdrop (airdrop-serve.py, MIT): plists in binary
format, /Ask is held open until the user decides (200 = accept, 403 =
decline), a links-only Ask is the whole transfer, /Upload carries a
gzip/DVZip-compressed cpio archive (archive.py). Only "Everyone" mode: there
is no Apple identity, so "Contacts Only" senders never see us.

Hardening on top: an /Upload is only taken from an address whose /Ask was
accepted in the last 10 minutes, the decompressed size is bounded by the free
disk space, and files go through IncomingFileWriter (no traversal, no
overwrite, .part until complete).
"""

from __future__ import annotations

import asyncio
import json
import logging
import plistlib
import shutil
import tempfile
import time
from pathlib import Path

from aiohttp import web

from multidrop.core import (EventHandler, IncomingFileWriter, IncomingOffer, OfferFile,
                            OfferHandler, TransferError, _noop_event)

from . import archive
from .http import AirDropRunner

log = logging.getLogger(__name__)

ACCEPT_VALID = 600            # seconds between accepted /Ask and /Upload
MAX_PLIST = 1 << 20
MIN_FREE = 512 * 1024 * 1024  # keep this much disk space free
# No codecs advertised: senders convert to JPEG/H.264 instead of HEIC/HEVC.
MEDIA_CAPABILITIES = json.dumps({"Version": 1}).encode()


def _plist(data: dict) -> web.Response:
    return web.Response(body=plistlib.dumps(data, fmt=plistlib.FMT_BINARY),
                        content_type="application/octet-stream")


async def _read_plist(request: web.Request) -> dict:
    body = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > MAX_PLIST:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_PLIST, actual_size=len(body))
    if not body:
        return {}
    try:
        data = plistlib.loads(body)
    except Exception:
        raise web.HTTPBadRequest(text="bad plist") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="plist is not a dictionary")
    return data


# FileType of a text "file" (iOS sends shared text as one .txt).
TEXT_TYPES = {"public.plain-text", "public.utf8-plain-text", "public.text"}
MAX_TEXT = 64 * 1024


def _size(value, default=0):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


class AirDropServer:
    def __init__(self, *, name: str, model: str, download_dir: Path, host: str, port: int,
                 ssl_context, on_offer: OfferHandler | None, on_event: EventHandler = _noop_event):
        self.name, self.model = name, model
        self.download_dir = download_dir
        self.host, self.port = host, port
        self.ssl_context = ssl_context
        self.on_offer = on_offer
        self.on_event = on_event
        # address -> (deadline, sender, single text file expected)
        self._accepted: dict[str, tuple[float, str, bool]] = {}
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.add_routes([web.post("/Discover", self._discover),
                        web.post("/Ask", self._ask),
                        web.post("/Upload", self._upload)])
        self._runner = AirDropRunner(app, handle_signals=False, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port, ssl_context=self.ssl_context)
        try:
            await site.start()
        except BaseException:
            await self._runner.cleanup()
            self._runner = None
            raise
        self.port = next(a[1] for a in self._runner.addresses)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _answer(self) -> dict:
        return {"ReceiverComputerName": self.name, "ReceiverModelName": self.model,
                "ReceiverMediaCapabilities": MEDIA_CAPABILITIES, "IsAirDropable": True}

    async def _discover(self, request: web.Request) -> web.Response:
        await _read_plist(request)
        return _plist(self._answer())

    async def _ask(self, request: web.Request) -> web.Response:
        ask = await _read_plist(request)
        address = request.remote or "?"
        sender = str(ask.get("SenderComputerName") or "Apple device")[:100]
        files = ask.get("Files") if isinstance(ask.get("Files"), list) else []
        items = ask.get("Items") if isinstance(ask.get("Items"), list) else []
        links = [i for i in items if isinstance(i, str) and i.startswith(("http://", "https://"))]
        if len(files) > archive.MAX_MEMBERS:
            return web.Response(status=413)
        offer_files = [OfferFile(id=str(i), name=str(f.get("FileName") or "unnamed"),
                                 size=_size(f.get("FileSize")),
                                 mime=str(f.get("FileType") or "") or None,
                                 size_known=_size(f.get("FileSize"), None) is not None)
                       for i, f in enumerate(files) if isinstance(f, dict)]
        if not offer_files:
            offer_files = [OfferFile(id=f"url{i}", name="link", size=0, text=u)
                           for i, u in enumerate(links)]
        if not offer_files:
            return web.Response(status=400, text="nothing offered")
        offer = IncomingOffer("airdrop", sender, address, offer_files)
        if self.on_offer is None:
            accepted = set()
        else:
            accepted = await self._consent(request, offer)
        if not accepted:
            self.on_event("rejected", {"from": sender})
            return web.Response(status=403)
        if not files:                        # links: the Ask is the transfer
            for u in links:
                self.on_event("message", {"from": sender, "text": u, "kind": "url"})
            self.on_event("session_done", {"from": sender})
        else:
            # A single small plain-text file is shown as a message as well.
            text = len(offer_files) == 1 and offer_files[0].mime in TEXT_TYPES and \
                offer_files[0].size <= MAX_TEXT
            self._accepted[address] = (time.monotonic() + ACCEPT_VALID, sender, text)
            self.on_event("session_started", {"from": sender, "files": len(offer_files),
                                              "size": offer.total_size})
        return _plist(self._answer())

    async def _consent(self, request: web.Request, offer: IncomingOffer) -> set[str]:
        """Ask the user; give up if the sender closes the connection."""
        decision = asyncio.ensure_future(self.on_offer(offer))
        try:
            while not decision.done():
                await asyncio.wait({decision}, timeout=0.5)
                transport = request.transport
                if transport is None or transport.is_closing():
                    self.on_event("cancelled", {"from": offer.sender_name})
                    return set()
            return decision.result()
        finally:
            decision.cancel()

    async def _upload(self, request: web.Request) -> web.Response:
        address = request.remote or "?"
        deadline, sender, text = self._accepted.pop(address, (0.0, "", False))
        if deadline < time.monotonic():
            return web.Response(status=403, text="no accepted /Ask")
        ctype = request.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype not in ("application/x-cpio", "application/x-dvzip"):
            return web.Response(status=415)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        limit = shutil.disk_usage(self.download_dir).free - MIN_FREE
        if limit <= 0:
            return web.Response(status=507)
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryFile(dir=self.download_dir) as raw:
            received = 0
            async for chunk in request.content.iter_chunked(1 << 20):
                received += len(chunk)
                if received > limit:
                    self.on_event("failed", {"from": sender, "error": "not enough space"})
                    return web.Response(status=413)
                raw.write(chunk)
                self.on_event("progress", {"file": "(AirDrop)", "done": received, "size": 0})
            raw.seek(0)

            def emit(event, d):
                loop.call_soon_threadsafe(self.on_event, event, d)
            try:
                saved = await asyncio.to_thread(self._extract, raw, limit, sender, emit)
            except archive.LimitExceeded as e:
                self.on_event("failed", {"from": sender, "error": str(e)})
                return web.Response(status=413)
            except (archive.ArchiveError, TransferError, OSError) as e:
                self.on_event("failed", {"from": sender, "error": str(e)})
                return web.Response(status=422)
        if text and len(saved) == 1 and saved[0].stat().st_size <= MAX_TEXT:
            self.on_event("message", {"from": sender, "kind": "text", "path": str(saved[0]),
                                      "text": saved[0].read_bytes().decode("utf-8", "replace")})
        self.on_event("session_done", {"from": sender, "files": len(saved)})
        return web.Response(status=200)

    def _extract(self, raw, limit: int, sender: str, emit) -> list[Path]:
        saved: list[Path] = []
        dest = self.download_dir

        class Sink:
            def __init__(self, member: archive.Member):
                self.w = IncomingFileWriter(dest, OfferFile(member.name, member.name,
                                                            member.size))

            def write(self, data):
                self.w.write(data)

            def abort(self):
                self.w.abort()

            def finish(self):
                path = self.w.finish()
                saved.append(path)
                emit("file_received", {"file": self.w.offer_file.name.removeprefix("./"),
                                       "path": str(path), "from": sender})
        # Files completed before an error stay (each one is size-checked);
        # the broken one is removed by its writer.
        archive.extract(archive.decode(raw), Sink, limit=limit)
        return saved
