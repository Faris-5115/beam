"""Interop with rquickshare (an independent implementation known to work with
Android). Needs the harness from ~/multidrop-research/rqs-harness, built with
cargo; skipped otherwise. The harness runs invisible: no mDNS, no BLE.
"""

import asyncio
import os
import socket
from pathlib import Path

import pytest

from multidrop.backends.quickshare import QuickShareBackend
from multidrop.core import TransferError

HARNESS = Path(os.environ.get(
    "RQS_HARNESS",
    Path.home() / "multidrop-research/rqs-target/release/rqs-harness"))

pytestmark = pytest.mark.skipif(not HARNESS.exists(), reason="rqs-harness not built")


class Events(list):
    def __call__(self, event, details):
        self.append((event, details))

    def of(self, name):
        return [d for e, d in self if e == name]


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _qs(make_config, alias):
    cfg = make_config(alias)
    cfg.quickshare.bind = "127.0.0.1"
    cfg.quickshare.mdns = False
    cfg.quickshare.ble = False
    return QuickShareBackend(cfg)


async def _wait_listening(port, timeout=10):
    for _ in range(int(timeout / 0.05)):
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError


async def _run_harness(*args):
    proc = await asyncio.create_subprocess_exec(
        str(HARNESS), *map(str, args), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    return proc


async def _harness_output(proc) -> str:
    out, _ = await asyncio.wait_for(proc.communicate(), 60)
    return out.decode()


async def test_multidrop_sends_to_rquickshare(make_config, tmp_path):
    port, inbox = _port(), tmp_path / "rqs-in"
    inbox.mkdir()
    proc = await _run_harness("recv", port, inbox)
    try:
        # rquickshare binds 0.0.0.0; probing it would count as a connection,
        # so wait for its READY line instead.
        assert (await proc.stdout.readline()).strip() == b"READY"
        src = tmp_path / "foto.jpg"
        src.write_bytes(os.urandom(1_300_000))
        sender = _qs(make_config, "multidrop")
        ev = Events()
        await sender.send(await sender.resolve("127.0.0.1", port), [src], ev)
        out = await _harness_output(proc)
    finally:
        if proc.returncode is None:
            proc.kill()
    assert "RESULT FINISHED" in out
    assert (inbox / "foto.jpg").read_bytes() == src.read_bytes()
    assert f'pin_code: Some("{ev.of("pin")[0]["pin"]}")' in out


async def test_rquickshare_rejects(make_config, tmp_path):
    port = _port()
    proc = await _run_harness("recv", port, tmp_path, "reject")
    try:
        assert (await proc.stdout.readline()).strip() == b"READY"
        src = tmp_path / "x.txt"
        src.write_text("x")
        sender = _qs(make_config, "multidrop")
        with pytest.raises(TransferError, match="rejected"):
            await sender.send(await sender.resolve("127.0.0.1", port), [src], Events())
    finally:
        proc.kill()
        await proc.wait()


async def test_rquickshare_sends_to_multidrop(make_config, tmp_path):
    receiver = _qs(make_config, "multidrop")
    ev = Events()
    done = asyncio.Event()

    def on_event(e, d):
        ev(e, d)
        if e in ("session_done", "failed"):
            done.set()

    async def accept(offer):
        return {f.id for f in offer.files}

    await receiver.start(accept, on_event)
    files = {"video.mp4": os.urandom(2_100_000), "note.txt": b"hello\n", "empty.bin": b""}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    try:
        proc = await _run_harness("send", f"127.0.0.1:{receiver.port}",
                                  *(tmp_path / n for n in files))
        out = await _harness_output(proc)
        await asyncio.wait_for(done.wait(), 10)
    finally:
        await receiver.stop()
    assert "RESULT FINISHED" in out
    assert ev.of("session_done"), ev
    for name, data in files.items():
        assert (receiver.config.download_dir / name).read_bytes() == data
    assert f'pin_code: Some("{ev.of("pin")[0]["pin"]}")' in out
