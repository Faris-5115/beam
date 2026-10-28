"""AirDrop upload bodies: a cpio archive, compressed.

Senders use different wrappers (see omdrop airdrop-serve.py, opendrop):
gzip (opendrop, older macOS; Content-Type application/x-cpio) or "DVZip"
(iOS; application/x-dvzip): blocks with a 4-byte big-endian header, top bit
set = stored, else a zlib stream of that many bytes. The archive inside is
cpio "odc" (070707); "newc" (070701/070702) is accepted too.

Everything here is synchronous and streaming; the server runs it in a thread.
"""

from __future__ import annotations

import gzip
import io
import os
import stat
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20
MAX_MEMBERS = 10_000
MAX_NAME = 4096
TRAILER = "TRAILER!!!"


class ArchiveError(ValueError):
    pass


class LimitExceeded(ArchiveError):
    pass


# -- decompression ----------------------------------------------------------

def _inflate_stream(read: Callable[[int], bytes], wbits: int) -> Iterator[bytes]:
    d = zlib.decompressobj(wbits)
    while not d.eof:
        data = read(CHUNK)
        if not data:
            raise ArchiveError("truncated compressed data")
        while data:
            out = d.decompress(data, CHUNK)
            if out:
                yield out
            data = d.unconsumed_tail
        if d.unused_data and wbits != 31:
            raise ArchiveError("compressed block has trailing data")
    tail = d.flush()
    if tail:
        yield tail


def _dvzip_blocks(f) -> Iterator[bytes]:
    while header := f.read(4):
        if len(header) != 4:
            raise ArchiveError("truncated DVZip block header")
        value = int.from_bytes(header, "big")
        size = value & 0x7FFFFFFF
        block = _Exact(f, size)
        if value & 0x80000000:
            while data := block.read(CHUNK):
                yield data
        else:
            yield from _inflate_stream(block.read, zlib.MAX_WBITS)
        if block.left:
            raise ArchiveError("truncated DVZip block")


class _Exact:
    """Reads at most `size` bytes of `f`."""

    def __init__(self, f, size: int):
        self.f, self.left = f, size

    def read(self, n: int) -> bytes:
        data = self.f.read(min(n, self.left)) if self.left else b""
        self.left -= len(data)
        return data


def decode(f) -> Iterator[bytes]:
    """Decompressed cpio bytes from a seekable upload body."""
    magic = f.read(6)
    f.seek(0)
    if magic[:2] == b"\x1f\x8b":
        yield from _gzip_members(f)
    elif magic.startswith(b"0707"):
        while data := f.read(CHUNK):
            yield data
    else:
        yield from _dvzip_blocks(f)


def _gzip_members(f) -> Iterator[bytes]:
    with gzip.GzipFile(fileobj=f) as gz:
        try:
            while data := gz.read(CHUNK):
                yield data
        except (OSError, EOFError) as e:
            raise ArchiveError(f"bad gzip data: {e}") from None


# -- cpio reading -----------------------------------------------------------

class _Stream:
    """Exact reads from an iterator of chunks, with an output limit."""

    def __init__(self, chunks: Iterator[bytes], limit: int):
        self._chunks = chunks
        self._buf = bytearray()
        self.total = 0
        self.limit = limit

    def read(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            self.total += len(chunk)
            if self.total > self.limit:
                raise LimitExceeded(f"archive larger than {self.limit} bytes")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def exact(self, n: int) -> bytes:
        data = self.read(n)
        if len(data) != n:
            raise ArchiveError("truncated archive")
        return data


@dataclass
class Member:
    name: str
    mode: int
    size: int

    @property
    def is_file(self) -> bool:
        return stat.S_ISREG(self.mode)

    @property
    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.mode)


def _header(s: _Stream) -> tuple[Member, int] | None:
    """Parse one header; returns the member and the data padding."""
    magic = s.exact(6)
    try:
        if magic == b"070707":
            h = s.exact(70)
            mode = int(h[12:18], 8)
            namesize = int(h[53:59], 8)
            size = int(h[59:70], 8)
            pad_name = pad_data = 0
        elif magic in (b"070701", b"070702"):
            h = s.exact(104)
            fields = [int(h[i:i + 8], 16) for i in range(0, 104, 8)]
            mode, size, namesize = fields[1], fields[6], fields[11]
            pad_name = (-(110 + namesize)) % 4
            pad_data = (-size) % 4
        else:
            raise ArchiveError(f"not a cpio header: {magic!r}")
    except ValueError as e:
        if isinstance(e, ArchiveError):
            raise
        raise ArchiveError("bad cpio header") from None
    if not 0 < namesize <= MAX_NAME:
        raise ArchiveError("bad cpio name length")
    name = s.exact(namesize)[:-1].decode("utf-8", "replace")
    s.exact(pad_name)
    if name == TRAILER:
        return None
    return Member(name, mode, size), pad_data


def extract(chunks: Iterator[bytes], open_file: Callable[[Member], object], *,
            limit: int) -> list[Member]:
    """Walk the archive; for every regular file call open_file(member), which
    returns an object with write()/finish() (or None to skip). Directories,
    links, devices and AppleDouble "._*" files are skipped."""
    s = _Stream(chunks, limit)
    members: list[Member] = []
    while True:
        parsed = _header(s)
        if parsed is None:
            return members
        member, pad = parsed
        if len(members) >= MAX_MEMBERS:
            raise LimitExceeded(f"more than {MAX_MEMBERS} archive members")
        members.append(member)
        base = member.name.rstrip("/").rsplit("/", 1)[-1]
        sink = open_file(member) if member.is_file and not base.startswith("._") else None
        left = member.size
        try:
            while left:
                data = s.read(min(left, CHUNK))
                if not data:
                    raise ArchiveError(f"truncated data for {member.name!r}")
                left -= len(data)
                if sink is not None:
                    sink.write(data)
        except BaseException:
            if sink is not None:
                sink.abort()
            raise
        if sink is not None:
            sink.finish()
        s.exact(pad)


# -- cpio writing (sending) -------------------------------------------------

def _odc_header(name: str, mode: int, size: int, ino: int, mtime: int) -> bytes:
    raw = name.encode() + b"\0"
    return (b"070707" + b"%06o%06o%06o%06o%06o%06o%06o%011o%06o%011o" % (
        0, ino & 0o777777, mode, 0, 0, 1, 0, mtime, len(raw), size)) + raw


def bom_entries(paths: list[Path]) -> list[tuple[Path, str]]:
    """(path, "./relative name") for files and folders, folders first."""
    out = []
    for p in paths:
        if p.is_dir():
            out.append((p, f"./{p.name}"))
            for root, dirs, files in os.walk(p):
                dirs.sort()
                rel_root = Path(root).relative_to(p.parent).as_posix()
                for d in dirs:
                    out.append((Path(root) / d, f"./{rel_root}/{d}"))
                for n in sorted(files):
                    full = Path(root) / n
                    if full.is_file():
                        out.append((full, f"./{rel_root}/{n}"))
        else:
            out.append((p, f"./{p.name}"))
    return out


def cpio_gzip(entries: list[tuple[Path, str]], level: int = 1) -> Iterator[bytes]:
    """gzip-compressed odc cpio of the entries, in chunks."""
    buf = io.BytesIO()
    gz = gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=level, mtime=0)

    def drain() -> bytes:
        data = buf.getvalue()
        buf.seek(0)
        buf.truncate()
        return data

    for ino, (path, name) in enumerate(entries, 1):
        st = path.stat()
        if path.is_dir():
            gz.write(_odc_header(name, stat.S_IFDIR | 0o755, 0, ino, int(st.st_mtime)))
            continue
        gz.write(_odc_header(name, stat.S_IFREG | 0o644, st.st_size, ino, int(st.st_mtime)))
        with open(path, "rb") as f:
            left = st.st_size
            while left and (data := f.read(min(CHUNK, left))):
                gz.write(data)
                left -= len(data)
                if buf.tell() >= CHUNK:
                    yield drain()
            if left:
                raise ArchiveError(f"{path} shrank while sending")
    gz.write(_odc_header(TRAILER, 0, 0, 0, 0))
    gz.close()
    yield drain()
