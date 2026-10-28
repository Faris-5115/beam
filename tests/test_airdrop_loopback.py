"""AirDrop protocol side on loopback (no AWDL radio): two multidrop
instances over HTTPS, archive formats from different senders, hostile input."""

import asyncio
import gzip
import io
import zlib

import aiohttp
import pytest

from multidrop.backends.airdrop import AirDropBackend, archive
from multidrop.backends.airdrop.server import AirDropServer
from multidrop.core import TransferError


class Events(list):
    def __call__(self, event, details):
        self.append((event, details))

    def of(self, name):
        return [d for e, d in self if e == name]


async def accept_all(offer):
    return {f.id for f in offer.files}


async def reject_all(offer):
    return set()


@pytest.fixture
async def airdrop(make_config):
    started = []

    async def make(on_offer=accept_all, host="127.0.0.1", mdns=False):
        a_cfg, b_cfg = make_config("Linux-PC"), make_config("Mac")
        for cfg in (a_cfg, b_cfg):
            cfg.airdrop.bind = host
            cfg.airdrop.port = 0
            cfg.airdrop.mdns = mdns
            cfg.airdrop.mdns_addresses = ["127.0.0.1"]
        a, b = AirDropBackend(a_cfg), AirDropBackend(b_cfg)
        events = Events()
        await b.start(on_offer, events)
        started.extend([a, b])
        return a, b, events

    yield make
    for backend in started:
        await backend.stop()


async def test_send_files_and_folder(airdrop, tmp_path):
    a, b, events = await airdrop()
    album = tmp_path / "Album"
    (album / "Day 2").mkdir(parents=True)
    (album / "Day 2" / "IMG_1.jpg").write_bytes(b"\xff\xd8" + bytes(range(256)) * 9000)
    (album / "empty.txt").write_bytes(b"")
    doc = tmp_path / "Letter.pdf"
    doc.write_bytes(b"%PDF-1.4 hello")
    device = await a.resolve("127.0.0.1", b.port)
    assert device.name == "Mac" and device.model == "MacBookPro18,3"
    sent = Events()
    await a.send(device, [album, doc], sent)
    inbox = b.config.download_dir
    assert (inbox / "Album" / "Day 2" / "IMG_1.jpg").read_bytes() == \
        (album / "Day 2" / "IMG_1.jpg").read_bytes()
    assert (inbox / "Album" / "empty.txt").read_bytes() == b""
    assert (inbox / "Letter.pdf").read_bytes() == doc.read_bytes()
    assert len(events.of("file_received")) == 3 and events.of("session_done")
    assert [e for e, _ in sent][-1] == "done"


async def test_rejected(airdrop, tmp_path):
    a, b, events = await airdrop(on_offer=reject_all)
    f = tmp_path / "x.txt"
    f.write_text("x")
    with pytest.raises(TransferError, match="rejected"):
        await a.send(await a.resolve("127.0.0.1", b.port), [f])
    assert events.of("rejected") and not list(b.config.download_dir.glob("*"))


async def test_sender_presents_client_certificate(airdrop, tmp_path):
    """A peer requiring mTLS must accept Discover, Ask and Upload alike."""
    import ssl
    a, b, events = await airdrop()
    b.server.ssl_context.load_verify_locations(cafile=str(a.identity.cert_path))
    b.server.ssl_context.verify_mode = ssl.CERT_REQUIRED
    f = tmp_path / "certificate.txt"
    f.write_text("authenticated TLS client")
    device = await a.resolve("127.0.0.1", b.port)
    await a.send(device, [f])
    assert (b.config.download_dir / f.name).read_bytes() == f.read_bytes()
    assert events.of("session_done")


async def test_discover_has_own_connection_ask_and_upload_share_one(airdrop, tmp_path, monkeypatch):
    import plistlib
    from multidrop.backends.airdrop.server import _plist
    seen = []
    original_ask, original_upload = AirDropServer._ask, AirDropServer._upload

    async def discover(self, request):
        seen.append(("discover", request.transport))
        assert plistlib.loads(await request.read())["DeviceSupportFlags"] == 111611
        return _plist(self._answer())

    async def ask(self, request):
        seen.append(("ask", request.transport))
        assert all(t is not request.transport for name, t in seen if name == "discover")
        return await original_ask(self, request)

    async def upload(self, request):
        seen.append(("upload", request.transport))
        assert seen[-2] == ("ask", request.transport)
        return await original_upload(self, request)

    monkeypatch.setattr(AirDropServer, "_discover", discover)
    monkeypatch.setattr(AirDropServer, "_ask", ask)
    monkeypatch.setattr(AirDropServer, "_upload", upload)
    a, b, _ = await airdrop()
    f = tmp_path / "fresh.txt"
    f.write_text("fresh TLS connection")
    target = await a.resolve("127.0.0.1", b.port)
    await a.send(target, [f])
    assert [n for n, _ in seen] == ["discover", "discover", "ask", "upload"]
    assert seen[0][1] is not seen[1][1]
    assert (b.config.download_dir / f.name).read_bytes() == f.read_bytes()


async def test_send_waits_for_receiver_to_wake(make_config, tmp_path, monkeypatch):
    from conftest import free_port
    from multidrop.backends import airdrop as ad
    from multidrop.core import Device
    monkeypatch.setattr(ad, "WAKE_RETRY", 0.03)
    cfg = make_config("sleeping Mac")
    cfg.airdrop.bind = "127.0.0.1"
    cfg.airdrop.port = free_port()
    cfg.airdrop.mdns = False
    a, b = AirDropBackend(make_config("sender")), AirDropBackend(cfg)
    target = Device("airdrop", "mac", "sleeping Mac", "127.0.0.1", cfg.airdrop.port)
    f = tmp_path / "wake.txt"
    f.write_text("arrived after wake-up")

    async def wake():
        await asyncio.sleep(0.12)
        await b.start(accept_all)

    task = asyncio.create_task(wake())
    try:
        await a.send(target, [f])
        assert (b.config.download_dir / f.name).read_bytes() == f.read_bytes()
    finally:
        await task
        await a.stop()
        await b.stop()


async def test_apple_http11_without_host_and_fragmented_plist(airdrop):
    import plistlib
    a, b, events = await airdrop()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", b.port, ssl=a.identity.client_ssl_context())

    async def request(path, body):
        data = plistlib.dumps(body, fmt=plistlib.FMT_BINARY)
        # A real Apple peer omits Host; keep-alive must survive more than
        # one request, including bodies split across incoming TCP packets.
        writer.write(f"POST {path} HTTP/1.1\r\nContent-Length: {len(data)}\r\n\r\n".encode())
        writer.write(data[:8])
        await writer.drain()
        await asyncio.sleep(0.03)
        writer.write(data[8:])
        await writer.drain()
        headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        length = next(int(l.split(b":", 1)[1]) for l in headers.split(b"\r\n")
                      if l.lower().startswith(b"content-length:"))
        payload = await reader.readexactly(length)
        assert headers.startswith(b"HTTP/1.1 200"), (headers, payload)
        return plistlib.loads(payload)

    try:
        assert (await request("/Discover", {}))["ReceiverComputerName"] == "Mac"
        await request("/Ask", {"SenderComputerName": "iPhone",
                               "Items": ["https://example.org/airdrop"]})
        assert events.of("message")[0]["text"] == "https://example.org/airdrop"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_airdrop_still_rejects_ambiguous_http_framing(airdrop):
    a, b, _ = await airdrop()
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", b.port, ssl=a.identity.client_ssl_context())
    try:
        writer.write(b"POST /Discover HTTP/1.1\r\nContent-Length: 0\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.readline(), 3)).startswith(b"HTTP/1.0 400")
    finally:
        writer.close()
        await writer.wait_closed()


async def test_url_and_text(airdrop):
    a, b, events = await airdrop()
    device = await a.resolve("127.0.0.1", b.port)
    await a.send(device, [], text="https://example.org/a?b=c")
    assert events.of("message") == [{"from": "Linux-PC", "text": "https://example.org/a?b=c",
                                     "kind": "url"}]
    await a.send(device, [], text="Hello world")
    assert (b.config.download_dir / "Hello world.txt").read_text() == "Hello world"
    assert events.of("message")[-1]["text"] == "Hello world"     # also shown as text


async def test_ipv6(airdrop, tmp_path):
    a, b, events = await airdrop(host="::1")
    f = tmp_path / "v6.bin"
    f.write_bytes(b"6" * 1000)
    await a.send(await a.resolve("::1", b.port), [f])
    assert (b.config.download_dir / "v6.bin").read_bytes() == b"6" * 1000


async def test_mdns_discovery(airdrop):
    a, b, _ = await airdrop(mdns=True)
    devices = await a.discover(1.5)
    assert [(d.name, d.port) for d in devices] == [("Mac", b.port)]


def test_missing_interface(make_config):
    cfg = make_config("x")
    cfg.airdrop.interface = "nonexistent0"
    with pytest.raises(TransferError, match="nonexistent0"):
        AirDropBackend(cfg)._bind()


# -- raw protocol: hostile or unusual senders ---------------------------------

def newc(entries):
    """cpio newc (070701) with 4-byte padding: [(name, mode, data)]."""
    out = io.BytesIO()
    for name, mode, data in entries + [("TRAILER!!!", 0, b"")]:
        raw = name.encode() + b"\0"
        fields = [0, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(raw), 0]
        out.write(b"070701" + b"".join(b"%08X" % f for f in fields) + raw)
        out.write(b"\0" * ((-(110 + len(raw))) % 4) + data + b"\0" * ((-len(data)) % 4))
    return out.getvalue()


def odc(entries):
    out = io.BytesIO()
    for name, mode, data in entries + [("TRAILER!!!", 0, b"")]:
        out.write(archive._odc_header(name, mode, len(data), 1, 0) + data)
    return out.getvalue()


def dvzip(payload, block=1000):
    """Alternate stored and zlib-compressed blocks, as iOS might."""
    out = b""
    for i in range(0, len(payload), block):
        part = payload[i:i + block]
        if (i // block) % 2:
            out += (0x80000000 | len(part)).to_bytes(4, "big") + part
        else:
            z = zlib.compress(part)
            out += len(z).to_bytes(4, "big") + z
    return out


@pytest.fixture
async def raw_server(tmp_path):
    from multidrop.backends.localsend.identity import Identity
    ident = Identity.load_or_create(tmp_path / "id", common_name="t")
    events = Events()
    server = AirDropServer(name="PC", model="MacBookPro18,3", download_dir=tmp_path / "in",
                           host="127.0.0.1", port=0, ssl_context=ident.server_ssl_context(),
                           on_offer=accept_all, on_event=events)
    await server.start()
    http = aiohttp.ClientSession()
    base = f"https://127.0.0.1:{server.port}"

    async def ask():
        import plistlib
        body = plistlib.dumps({"SenderComputerName": "iPhone", "Files": [
            {"FileName": "a", "FileType": "public.data"}]}, fmt=plistlib.FMT_BINARY)
        async with http.post(base + "/Ask", data=body, ssl=False) as r:
            assert r.status == 200

    async def upload(body, ctype="application/x-dvzip", do_ask=True):
        if do_ask:
            await ask()
        async with http.post(base + "/Upload", data=body, ssl=False,
                             headers={"Content-Type": ctype}) as r:
            return r.status

    yield upload, tmp_path / "in", events
    await http.close()
    await server.stop()


REG, DIR, LNK = 0o100644, 0o040755, 0o120777


async def test_upload_formats(raw_server):
    upload, inbox, _ = raw_server
    big = bytes(range(256)) * 50
    assert await upload(dvzip(newc([("./d", DIR, b""), ("./d/n.bin", REG, big)]))) == 200
    assert (inbox / "d" / "n.bin").read_bytes() == big
    assert await upload(gzip.compress(odc([("./g.txt", REG, b"gzip")])),
                        "application/x-cpio") == 200
    assert (inbox / "g.txt").read_bytes() == b"gzip"
    assert await upload(odc([("./plain.txt", REG, b"raw")]), "application/x-cpio") == 200
    assert (inbox / "plain.txt").read_bytes() == b"raw"


async def test_upload_hostile_names_and_types(raw_server):
    upload, inbox, events = raw_server
    body = gzip.compress(odc([
        ("../../../escape.txt", REG, b"1"), ("/etc/abs.txt", REG, b"2"),
        ("./._IMG.jpg", REG, b"appledouble"), ("./link", LNK, b"/etc/passwd"),
        ("./ok.txt", REG, b"3")]))
    assert await upload(body) == 200
    files = sorted(p.relative_to(inbox).as_posix() for p in inbox.rglob("*") if p.is_file())
    assert files == ["escape.txt", "etc/abs.txt", "ok.txt"]
    assert not (inbox.parent / "escape.txt").exists()


async def test_upload_needs_accepted_ask(raw_server):
    upload, inbox, _ = raw_server
    body = gzip.compress(odc([("./x", REG, b"x")]))
    assert await upload(body, do_ask=False) == 403
    assert await upload(body, "text/plain") == 415
    assert await upload(body, do_ask=False) == 403      # the Ask was used up above
    assert not inbox.exists() or not any(inbox.iterdir())


async def test_upload_broken_archives(raw_server, monkeypatch):
    upload, inbox, events = raw_server
    whole = gzip.compress(odc([("./a.txt", REG, b"A" * 100), ("./b.txt", REG, b"B" * 100)]))
    assert await upload(whole[:-30]) == 422               # truncated gzip
    assert await upload(b"0707" + b"x" * 200) == 422      # garbage header
    lying = odc([("./big", REG, b"C" * 10)]).replace(b"00000000012", b"00000100000", 1)
    assert await upload(lying, "application/x-cpio") == 422   # data shorter than declared
    assert not list(inbox.rglob("*.part"))
    monkeypatch.setattr("multidrop.backends.airdrop.server.MIN_FREE", 0)
    monkeypatch.setattr("shutil.disk_usage", lambda p: type("U", (), {"free": 5000})())
    bomb = gzip.compress(odc([("./zeros", REG, bytes(100_000))]))   # tiny on the wire
    assert len(bomb) < 5000
    assert await upload(bomb) == 413
    assert events.of("failed")


def test_cpio_roundtrip(tmp_path):
    (tmp_path / "src" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "sub" / "f").write_bytes(b"F" * 3_000_000)
    entries = archive.bom_entries([tmp_path / "src"])
    assert [n for _, n in entries] == ["./src", "./src/sub", "./src/sub/f"]
    data = b"".join(archive.cpio_gzip(entries))
    got = {}

    class Sink:
        def __init__(self, m):
            self.name, self.buf = m.name, bytearray()

        def write(self, d):
            self.buf += d

        def finish(self):
            got[self.name] = bytes(self.buf)

        def abort(self):
            pass
    members = archive.extract(archive.decode(io.BytesIO(data)), Sink, limit=10**8)
    assert [m.name for m in members] == ["./src", "./src/sub", "./src/sub/f"]
    assert got == {"./src/sub/f": b"F" * 3_000_000}


async def test_daemon_survives_missing_awdl(make_config, tmp_path, monkeypatch):
    from multidrop.daemon.service import Daemon
    cfg = make_config("pc")
    cfg.localsend.bind = cfg.quickshare.bind = "127.0.0.1"
    cfg.quickshare.mdns = False
    cfg.airdrop.enabled = True
    cfg.airdrop.interface = "nonexistent0"
    d = Daemon(cfg)
    await d.start()
    try:
        assert set(d.backends) == {"localsend", "quickshare"}
        started = d.history[-1]
        assert "nonexistent0" in started["failed"]["airdrop"]
    finally:
        await d.stop()
