"""Use omdrop-owl's packaged radio helper as a separate program.

Only the explicit `airdrop run` command changes the radio. Normal daemon
startup never invokes pkexec. The upstream helper owns profiles, AWDL,
announcements, its time limit and Wi-Fi restoration.
"""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from pathlib import Path

from multidrop.config import Config
from multidrop.core import TransferError

HELPER = Path("/usr/lib/omdrop/omdrop-discoverable")


class OwlRadio:
    def __init__(self):
        self.owned = False

    async def _call(self, *args: str, privileged: bool = False,
                    timeout: float = 5) -> tuple[int, str]:
        if not HELPER.is_file():
            raise TransferError("omdrop-owl is not installed: install the .deb first "
                                "(see docs/INSTALL-ROOT.md)")
        argv = [str(HELPER), *args]
        if privileged:
            if not shutil.which("pkexec"):
                raise TransferError("pkexec is missing; install omdrop-owl's dependencies")
            argv.insert(0, "pkexec")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            raise TransferError(f"cannot run omdrop-owl: {e}") from e
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except (TimeoutError, asyncio.CancelledError) as e:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            if isinstance(e, asyncio.CancelledError):
                raise
            raise TransferError(f"omdrop-owl {args[0]} did not answer in time") from e
        if proc.returncode not in (0, 5) or (proc.returncode == 5 and args[0] != "start"):
            detail = stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            raise TransferError(f"omdrop-owl {args[0]} failed ({proc.returncode}): {detail}")
        return proc.returncode, stdout.decode(errors="replace")

    async def _json(self, command: str) -> dict:
        _, raw = await self._call(command, "--json")
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("expected object")
        except ValueError as e:
            raise TransferError(f"invalid omdrop-owl {command} response") from e
        return data

    async def probe(self) -> dict:
        data = await self._json("probe")
        if data.get("backend") != "owl":
            raise TransferError("installed radio backend is not omdrop-owl")
        if not isinstance(data.get("missing"), list) or not isinstance(data.get("hardware"), bool):
            raise TransferError("invalid omdrop-owl probe response")
        return data

    async def status(self) -> dict:
        data = await self._json("status")
        if not isinstance(data.get("visible"), bool):
            raise TransferError("invalid omdrop-owl status response")
        return data

    async def start(self, seconds: int) -> None:
        if not 60 <= seconds <= 3600:
            raise TransferError("AirDrop window must be 1..60 minutes")
        probe = await self.probe()
        if probe["missing"] or not probe["hardware"]:
            reasons = [str(item.get("say", item)) for item in probe["missing"]]
            raise TransferError("; ".join(reasons) or "no supported AirDrop radio")
        if (await self.status())["visible"]:
            raise TransferError("AirDrop radio is already in use; stop the existing session first")
        # On failure the upstream helper rolls back partial setup. Its own
        # finite supervisor window also survives a crash of multidrop.
        code, _ = await self._call("start", str(seconds), privileged=True, timeout=120)
        if code == 5:
            # Another process won the start race; do not tear its session down.
            raise TransferError("AirDrop radio was started by another process")
        self.owned = True

    async def stop(self) -> None:
        if self.owned:
            await self._call("stop", privileged=True, timeout=90)
            self.owned = False


def session_config(config: Config) -> Config:
    cfg = copy.deepcopy(config)
    # These must agree with omdrop-discoverable's raw mDNS announcer.
    cfg.airdrop.enabled = True
    cfg.airdrop.interface = "awdl0"
    cfg.airdrop.bind = ""
    cfg.airdrop.port = 8771
    cfg.airdrop.service_id = "mac"
    cfg.airdrop.mdns = False
    cfg.airdrop.mdns_addresses = []
    return cfg


async def _watch(radio: OwlRadio, stop: asyncio.Event, seconds: int) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
            return
        except TimeoutError:
            pass
        if asyncio.get_running_loop().time() >= deadline or not (await radio.status())["visible"]:
            stop.set()


async def run_session(config: Config, minutes: int, open_ui: bool = False,
                      wifi=None) -> None:
    """`wifi`: a wifi_channel.WifiPin that moves the station to an AirDrop
    channel of the same network first and back afterwards. Only when the user
    asked for it (`--switch-channel`); None leaves the Wi-Fi alone."""
    from multidrop import daemon

    # Check BEFORE starting the radio. An existing daemon owns the API and
    # receiver ports; silently falling back would leave AirDrop unserved.
    try:
        daemon._check_not_running(daemon.socket_path(config))
    except daemon.AlreadyRunning as e:
        raise TransferError(f"{e}; stop multidropd before `multidrop airdrop run`") from e
    radio = OwlRadio()
    stop = asyncio.Event()
    started = asyncio.get_running_loop().create_future()
    tasks = []
    server = None
    try:
        if wifi is not None:
            note = await wifi.prepare(minutes)
            if note:
                print(note, flush=True)
        print("Starting omdrop-owl; Wi-Fi may reconnect. Keep this terminal open.", flush=True)
        await radio.start(minutes * 60)
        cfg = session_config(config)
        # Construct the daemon only now: awdl0's MAC is our service identity.
        server = asyncio.create_task(daemon.run(cfg, stop=stop, started=started))
        tasks.append(server)
        done, _ = await asyncio.wait([server, started], return_when=asyncio.FIRST_COMPLETED)
        if server in done:
            await server
            raise TransferError("AirDrop receiver stopped before startup")
        # daemon.run tolerates individual backend errors. Verify that AirDrop
        # actually made it, rather than showing a working LocalSend-only UI.
        from multidrop.daemon.client import DaemonClient
        async with DaemonClient(daemon.socket_path(cfg)) as client:
            status = await client.get("/api/status")
        if "airdrop" not in status["protocols"]:
            raise TransferError("AirDrop receiver failed to start; see the daemon log")
        url = started.result()
        print(f"AirDrop ready for {minutes} min. Web UI: {url}", flush=True)
        if open_ui:
            daemon.open_ui(url)
        watcher = asyncio.create_task(_watch(radio, stop, minutes * 60))
        tasks.append(watcher)
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        stop.set()
        for task in tasks:
            if task is not server:
                task.cancel()
        # Let daemon.run finish its own shutdown. Cancelling it after the
        # stop event fired can interrupt its finally block and leak sockets.
        await asyncio.gather(*tasks, return_exceptions=True)
        started.cancel()
        try:
            await radio.stop()
        finally:
            if wifi is not None:
                await wifi.restore()


async def command(args, cfg: Config) -> int:
    if args.airdrop_command == "run":
        wifi = None
        if args.switch_channel:
            from multidrop.wifi_channel import WifiPin
            wifi = WifiPin()
        await run_session(cfg, args.minutes, args.open, wifi)
        return 0
    radio = OwlRadio()
    probe = await radio.probe()
    status = await radio.status()
    print(json.dumps({"probe": probe, "status": status}, ensure_ascii=False, indent=2))
    return 0 if probe["hardware"] and not probe["missing"] else 1
