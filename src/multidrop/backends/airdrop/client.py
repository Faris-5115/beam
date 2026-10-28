"""AirDrop sender: /Discover, /Ask (wait for the receiver to accept), /Upload.

The backend uses a short-lived connection for Discover, and passes a fresh
session to send() for Ask/Upload. Apple's Discover connection cannot be reused.

As opendrop's client: binary plists, the upload is a gzip-compressed odc cpio
with "./name" paths (Content-Type application/x-cpio), sent chunked. URLs go
in the Ask's "Items" and need no upload.

The Ask has the fields today's senders use (observed from macOS/iOS 26 by
omdrop, see docs/RESEARCH.md): TransferID, TransferType (files/links), Items,
per file FileSize, FileIcon (JPEG preview, only if Pillow is installed). The
upload carries TotalBytes and TransferID headers like a Mac's.
"""

from __future__ import annotations

import asyncio
import io
import mimetypes
import plistlib
import uuid
from pathlib import Path

import aiohttp
import yarl

from multidrop.core import EventHandler, TransferError

from . import archive

ASK_TIMEOUT = 300          # the receiver's user has this long to accept
UTI = {"image/jpeg": "public.jpeg", "image/png": "public.png", "image/heic": "public.heic",
       "video/mp4": "public.mpeg-4", "video/quicktime": "com.apple.quicktime-movie",
       "application/pdf": "com.adobe.pdf", "text/plain": "public.plain-text",
       "application/zip": "public.zip-archive"}


def uti_for(path: Path) -> str:
    if path.is_dir():
        return "public.folder"
    mime = mimetypes.guess_type(path.name)[0] or ""
    return UTI.get(mime, "public.data")


def size_of(path: Path) -> int:
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return path.stat().st_size


def file_icon(paths: list[Path]) -> bytes | None:
    """JPEG preview of the first image, as senders put into the Ask."""
    try:
        from PIL import Image
    except ImportError:
        return None
    for p in paths:
        if p.is_file() and (mimetypes.guess_type(p.name)[0] or "").startswith("image/"):
            try:
                with Image.open(p) as im:
                    im.thumbnail((240, 240))
                    buf = io.BytesIO()
                    im.convert("RGB").save(buf, "JPEG", quality=80)
                    return buf.getvalue()
            except Exception:
                return None
    return None


def build_ask(our_name: str, sender_id: str, model: str, paths: list[Path],
              urls: list[str], transfer_id: str) -> dict:
    ask = {"TransferID": {"id": transfer_id}, "SenderComputerName": our_name,
           "SenderModelName": model, "SenderID": sender_id, "ConvertMediaFormats": False}
    if paths:
        ask.update(TransferType={"files": {}}, BundleID="com.apple.finder", Items=[])
        ask["Files"] = [{"FileName": p.name, "FileType": uti_for(p), "FileSize": size_of(p),
                         "FileBomPath": f"./{p.name}", "FileIsDirectory": p.is_dir(),
                         "ConvertMediaFormats": False} for p in paths]
        icon = file_icon(paths)
        if icon:
            ask["FileIcon"] = icon
    else:
        ask.update(TransferType={"links": {}}, BundleID="com.apple.Safari", Items=list(urls))
    return ask


def url_for(host: str, port: int, path: str) -> yarl.URL:
    return yarl.URL.build(scheme="https", host=host, port=port, path=path)


async def _post(http: aiohttp.ClientSession, host: str, port: int, path: str, body: dict,
                timeout: float) -> tuple[int, dict]:
    data = plistlib.dumps(body, fmt=plistlib.FMT_BINARY)
    async with http.post(url_for(host, port, path), data=data,
                         headers={"Content-Type": "application/octet-stream",
                                  "User-Agent": "AirDrop/1.0"},
                         timeout=aiohttp.ClientTimeout(total=timeout)) as r:
        raw = await r.read()
    try:
        answer = plistlib.loads(raw) if raw else {}
    except Exception:
        answer = {}
    return r.status, answer if isinstance(answer, dict) else {}


async def discover(http: aiohttp.ClientSession, host: str, port: int) -> dict:
    status, answer = await _post(http, host, port, "/Discover",
                               {"DeviceSupportFlags": 111611}, 10)
    if status != 200:
        raise TransferError(f"/Discover: HTTP {status}")
    return answer


async def send(http: aiohttp.ClientSession, host: str, port: int, *, our_name: str,
               sender_id: str, model: str, paths: list[Path], urls: list[str],
               on_event: EventHandler, target: str) -> None:
    try:
        await _send(http, host, port, our_name, sender_id, model, paths, urls, on_event, target)
    except TimeoutError:
        raise TransferError(f"{target} did not answer in time") from None
    except (aiohttp.ClientError, OSError) as e:
        raise TransferError(f"connection to {target} failed: {e}") from None


async def _send(http, host, port, our_name, sender_id, model, paths, urls, on_event, target):
    # The backend has completed Discover on its separate, short-lived socket.
    transfer_id = str(uuid.uuid4()).upper()
    ask = build_ask(our_name, sender_id, model, paths, urls, transfer_id)
    on_event("waiting", {"to": target})
    status, _ = await _post(http, host, port, "/Ask", ask, ASK_TIMEOUT)
    if status != 200:
        on_event("rejected", {"to": target, "status": status})
        raise TransferError(f"rejected by receiver (HTTP {status})")
    if not paths:
        on_event("done", {"to": target, "files": len(urls)})
        return

    entries = archive.bom_entries(paths)
    total = sum(p.stat().st_size for p, _ in entries if p.is_file())

    async def body():
        chunks = archive.cpio_gzip(entries)
        sent = 0
        while (chunk := await asyncio.to_thread(next, chunks, None)) is not None:
            sent += len(chunk)
            on_event("progress", {"file": "AirDrop", "done": min(sent, total), "size": total})
            yield chunk

    async with http.post(url_for(host, port, "/Upload"), data=body(),
                         headers={"Content-Type": "application/x-cpio",
                                  "User-Agent": "AirDrop/1.0", "TotalBytes": str(total),
                                  "TransferID": transfer_id},
                         timeout=aiohttp.ClientTimeout(total=None, sock_read=120)) as r:
        if r.status != 200:
            raise TransferError(f"upload refused: HTTP {r.status}")
    for p in paths:
        on_event("file_sent", {"file": p.name})
    on_event("done", {"to": target, "files": len(paths)})
