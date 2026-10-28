"""LocalSend sender: the client side of the upload API (spec 4.x)."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import mimetypes
import re
import ssl
import uuid
from pathlib import Path

import aiohttp

from multidrop.core import Device, EventHandler, PinRequired, TransferError, collect_files

from .protocol import API, base_url, device_from_info

CHUNK = 256 * 1024
_HEX64 = re.compile(r"^[0-9A-Fa-f]{64}$")

_PREPARE_ERRORS = {
    401: "PIN required or wrong PIN",
    403: "rejected by receiver",
    409: "receiver is busy with another transfer",
    429: "too many attempts",
}


class ClientCertConnector(aiohttp.TCPConnector):
    """Uses our client certificate wherever aiohttp would use unverified TLS.

    Requests passing ssl=aiohttp.Fingerprint(...) still get aiohttp's
    certificate pinning check on top.
    """

    def __init__(self, client_context: ssl.SSLContext, **kwargs):
        super().__init__(**kwargs)
        self._client_context = client_context

    def _get_ssl_context(self, req):
        if req.is_ssl() and not isinstance(req.ssl, ssl.SSLContext) and req.ssl is not True:
            return self._client_context
        return super()._get_ssl_context(req)


def ssl_for(device: Device):
    """TLS setting for a peer: pin its certificate to the announced fingerprint.

    In HTTPS mode a LocalSend fingerprint is the SHA-256 of the certificate,
    so a peer that announced one must present that certificate. Peers whose
    fingerprint is not a certificate hash get unverified TLS, as in the
    official app.
    """
    if device.extra.get("scheme", "https") != "https":
        return True  # plain HTTP: no TLS involved
    if _HEX64.match(device.id):
        return aiohttp.Fingerprint(bytes.fromhex(device.id))
    return False


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


async def _file_metadata(path: Path, name: str) -> dict:
    st = path.stat()
    return {
        "id": uuid.uuid4().hex,
        "fileName": name,
        "size": st.st_size,
        "fileType": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "sha256": await asyncio.to_thread(_sha256_file, path),
        "preview": None,
        "metadata": {"modified": _iso(st.st_mtime), "accessed": _iso(st.st_atime)},
    }


async def _read_chunks(source: Path | bytes, name: str, size: int,
                       on_event: EventHandler):
    if isinstance(source, bytes):
        yield source
        on_event("progress", {"file": name, "done": size, "size": size})
        return
    sent = 0
    with open(source, "rb") as f:
        while chunk := await asyncio.to_thread(f.read, CHUNK):
            sent += len(chunk)
            yield chunk
            on_event("progress", {"file": name, "done": sent, "size": size})


async def fetch_info(http: aiohttp.ClientSession, address: str, port: int) -> Device:
    """Ask a peer at a known address who it is (GET /info), HTTPS first."""
    host = f"[{address}]" if ":" in address else address
    last_error: Exception | None = None
    for scheme in ("https", "http"):
        url = f"{scheme}://{host}:{port}{API}/info"
        try:
            async with http.get(url, ssl=False,
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
                r.raise_for_status()
                device = device_from_info(await r.json(content_type=None),
                                          address, port)
                device.port = port
                device.extra["scheme"] = scheme
                return device
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            last_error = e
    raise TransferError(f"no LocalSend device at {address}:{port}: {last_error}")


async def send_files(http: aiohttp.ClientSession, own_info: dict, device: Device,
                     paths: list[Path], on_event: EventHandler,
                     pin: str | None = None, text: str | None = None) -> None:
    entries = collect_files(paths) if paths or text is None else []
    on_event("preparing", {"files": len(entries)})
    meta = {}
    sources: dict[str, Path | bytes] = {}
    if text is not None:
        # A message is a text/plain "file" whose content rides in `preview`;
        # receivers answer 204 and need no upload.
        data = text.encode()
        fid = uuid.uuid4().hex
        meta[fid] = {"id": fid, "fileName": f"{fid}.txt", "size": len(data),
                     "fileType": "text/plain", "sha256": None, "preview": text}
        sources[fid] = data
    for path, name in entries:
        m = await _file_metadata(path, name)
        meta[m["id"]] = m
        sources[m["id"]] = path

    url = base_url(device)
    tls = ssl_for(device)
    params = {"pin": pin} if pin else {}
    on_event("waiting", {"to": device.name})
    # The receiver may wait for its user to accept, so no read timeout here.
    try:
        async with http.post(f"{url}/prepare-upload", params=params, ssl=tls,
                             json={"info": own_info, "files": meta},
                             timeout=aiohttp.ClientTimeout(total=None, connect=10)) as r:
            if r.status == 204:
                on_event("done", {"to": device.name, "files": len(meta)})
                return
            if r.status == 401:
                raise PinRequired(_PREPARE_ERRORS[401])
            if r.status != 200:
                raise TransferError(_PREPARE_ERRORS.get(
                    r.status, f"prepare-upload failed: HTTP {r.status}"))
            answer = await r.json(content_type=None)
    except aiohttp.ServerFingerprintMismatch:
        raise TransferError("receiver certificate does not match its fingerprint")
    except aiohttp.ClientError as e:
        raise TransferError(f"cannot reach {device.label()}: {e}")

    session_id = answer.get("sessionId")
    tokens = answer.get("files") or {}
    if not isinstance(session_id, str) or not isinstance(tokens, dict):
        raise TransferError("invalid prepare-upload response")

    try:
        for fid, token in tokens.items():
            m = meta.get(fid)
            if m is None:
                continue
            q = {"sessionId": session_id, "fileId": fid, "token": token}
            async with http.post(
                    f"{url}/upload", params=q, ssl=tls,
                    data=_read_chunks(sources[fid], m["fileName"], m["size"], on_event),
                    headers={"Content-Length": str(m["size"]),
                             "Content-Type": "application/octet-stream"},
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as r:
                if r.status != 200:
                    raise TransferError(
                        f"upload of {m['fileName']} failed: HTTP {r.status}")
            on_event("file_sent", {"file": m["fileName"]})
    except BaseException as e:
        try:
            async with http.post(f"{url}/cancel", params={"sessionId": session_id},
                                 ssl=tls, timeout=aiohttp.ClientTimeout(total=5)):
                pass
        except Exception:
            pass
        if isinstance(e, aiohttp.ClientError):
            raise TransferError(f"upload failed: {e}") from e
        raise
    skipped = len(meta) - len(tokens)
    on_event("done", {"to": device.name, "files": len(tokens), "skipped": skipped})
