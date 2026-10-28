"""Browser smoke test of the multidropd web UI, entirely on 127.0.0.1.

Starts a daemon (no notifications, discovery only on lo) and a Quick Share
"phone", then runs tools/dev/ui_smoke.mjs in headless Chromium. The page and this
script talk over stdin/stdout markers: READY_FOR_OFFER (Quick Share offer),
READY_FOR_MULTI (LocalSend offer of 3 files, the page accepts 2),
READY_FOR_PIN (LocalSend receiver with a PIN), READY_FOR_RESTART (daemon
restarted, history must still be there).

  PLAYWRIGHT_CORE=/path/node_modules/playwright-core \
  CHROMIUM=~/.cache/ms-playwright/chromium_headless_shell-XXXX/chrome-headless-shell-linux64/chrome-headless-shell \
  .venv/bin/python tools/dev/ui_smoke.py [SCREENSHOT_DIR]
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import aiohttp

from multidrop.backends.localsend import LocalSendBackend
from multidrop.backends.quickshare import QuickShareBackend, outbound
from multidrop.config import Config
from multidrop.daemon import run


def config(root: Path, alias: str) -> Config:
    cfg = Config()
    cfg.alias = alias
    cfg.download_dir = root / alias / "downloads"
    cfg.state_dir = root / alias / "state"
    cfg.localsend.bind = "127.0.0.1"
    cfg.localsend.port = 0
    cfg.localsend.multicast_interface = "127.0.0.1"
    cfg.localsend.multicast_port = 53399
    cfg.quickshare.bind = "127.0.0.1"
    cfg.quickshare.port = 0
    cfg.quickshare.mdns_addresses = ["127.0.0.1"]
    cfg.quickshare.ble = False
    cfg.localsend.share_port = 0
    cfg.localsend.share_bind = "127.0.0.1"
    cfg.daemon.web_port = 0
    cfg.daemon.history_file = str(root / alias / "history.jsonl")
    return cfg


async def daemon_port(pc: Config, protocol: str) -> int:
    from multidrop.daemon import socket_path
    async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(str(socket_path(pc)))) as s:
        async with s.get("http://localhost/api/status") as r:
            return (await r.json())["protocols"][protocol]["port"]


async def main(shots: str) -> int:
    root = Path(tempfile.mkdtemp(prefix="multidrop-ui-"))
    os.environ["XDG_RUNTIME_DIR"] = str(root / "rt")
    (root / "rt").mkdir(mode=0o700)
    pc, phone_cfg = config(root, "pc"), config(root, "phone")

    phone = QuickShareBackend(phone_cfg)

    async def accept_all(offer):
        return {f.id for f in offer.files}
    await phone.start(accept_all, lambda e, d: None)

    async def start_daemon():
        started, stop = asyncio.get_running_loop().create_future(), asyncio.Event()
        task = asyncio.ensure_future(run(pc, notifications=False, started=started, stop=stop))
        return await started, stop, task

    url, stop, daemon = await start_daemon()
    tablet = None
    tasks = set()

    async def reply(text: str) -> None:
        node.stdin.write(text.encode() + b"\n")
        await node.stdin.drain()

    async def multi_offer() -> None:
        files = []
        for name in ("eins.jpg", "zwei.jpg", "drei.jpg"):
            f = root / name
            f.write_bytes(os.urandom(15000))
            files.append(f)
        lcfg = config(root, "laptop")
        lcfg.localsend.port = 53398       # announced in the request; 0 is refused
        sender = LocalSendBackend(lcfg)
        try:
            device = await sender.resolve("127.0.0.1", await daemon_port(pc, "localsend"))
            await sender.send(device, files)
        finally:
            await sender.stop()
        got = sorted(p.name for p in pc.download_dir.iterdir() if p.suffix == ".jpg")
        print(f"PY daemon kept from 3 offered: {got}", flush=True)
        await reply("MULTI_DONE")

    here = Path(__file__).parent
    node = await asyncio.create_subprocess_exec(
        "node", str(here / "ui_smoke.mjs"), url, shots,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    try:
        async for raw in node.stdout:
            line = raw.decode().rstrip()
            print(line, flush=True)
            if line == "READY_FOR_OFFER":
                src = root / "vom-handy.jpg"
                src.write_bytes(b"\xff\xd8" + os.urandom(20000))
                await outbound.send("127.0.0.1", await daemon_port(pc, "quickshare"),
                                    our_name="Pixel 9", entries=[(src, src.name)],
                                    on_event=lambda e, d: None)
                ok = (pc.download_dir / src.name).read_bytes() == src.read_bytes()
                print(f"PY received in daemon download dir: {ok}", flush=True)
                await reply("RECEIVED")
            elif line == "READY_FOR_MULTI":
                t = asyncio.ensure_future(multi_offer())
                tasks.add(t)
            elif line == "READY_FOR_PIN":
                tcfg = config(root, "tablet")
                tcfg.localsend.pin = "2468"
                tablet = LocalSendBackend(tcfg)
                await tablet.start(accept_all, lambda e, d: None)
                await reply(f"PIN_TARGET {tablet.port} 2468")
            elif line == "READY_FOR_RESTART":
                stop.set()
                await daemon
                url, stop, daemon = await start_daemon()
                print("PY daemon restarted", flush=True)
                await reply(f"RESTARTED {url}")
        rc = await node.wait()
        got = (phone_cfg.download_dir / "gruss.txt").read_text()
        print(f"PY phone got gruss.txt: {got!r}")
        if tablet is not None:
            got = [p.name for p in tablet.config.download_dir.iterdir()]
            print(f"PY tablet (LocalSend with PIN) got: {got}")
        for t in tasks:
            await t
        return rc
    finally:
        stop.set()
        await daemon
        await phone.stop()
        if tablet is not None:
            await tablet.stop()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else tempfile.gettempdir())))
