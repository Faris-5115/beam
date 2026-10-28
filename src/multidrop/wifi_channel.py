"""Move the Wi-Fi to an AirDrop-compatible channel for one AirDrop session.

AWDL shares the station's channel; on the MT7925 omdrop-owl supports only
channel 6, 44 or 149 (2437/5220/5745 MHz). If the active network runs
elsewhere (here: 6 GHz) but the same SSID is also on one of those channels,
a temporary NetworkManager profile pinned to that BSSID is activated:
band, channel and BSSID fixed, autoconnect off. A plain `connection up ...
ap BSSID` is not enough, NM roams straight back to 6 GHz.

Restore = activate the original profile again and delete the temporary one.
It runs when the session ends and, should multidrop die, from a systemd
--user timer started before anything changes. Only `multidrop airdrop run`
(started by the user) uses this; it needs NM's settings.modify.system and
network-control permissions, which the local active user has on Ubuntu.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from multidrop.core import TransferError

log = logging.getLogger(__name__)

AIRDROP_FREQS = {2437: 6, 5220: 44, 5745: 149}
TEMP_PROFILE = "multidrop-airdrop-session"
RESTORE_UNIT = "multidrop-wifi-restore"

Runner = Callable[[list[str], float], Awaitable[tuple[int, str]]]


async def _run(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, f"{argv[0]} timed out"
    return proc.returncode, out.decode(errors="replace")


def split_terse(line: str) -> list[str]:
    """nmcli -t fields: ':' separated, '\\:' escapes a colon."""
    fields, cur, i = [], "", 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if line[i] == ":":
            fields.append(cur)
            cur = ""
        else:
            cur += line[i]
        i += 1
    fields.append(cur)
    return fields


@dataclass
class AccessPoint:
    active: bool
    ssid: str
    bssid: str
    freq: int
    signal: int


def parse_scan(text: str) -> list[AccessPoint]:
    """`nmcli -t -f ACTIVE,SSID,BSSID,FREQ,SIGNAL dev wifi list`"""
    aps = []
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) != 5:
            continue
        try:
            freq = int(f[3].split()[0])
            signal = int(f[4] or 0)
        except (ValueError, IndexError):
            continue
        aps.append(AccessPoint(f[0] in ("yes", "ja"), f[1], f[2].upper(), freq, signal))
    return aps


def choose(aps: list[AccessPoint]) -> tuple[AccessPoint, AccessPoint | None]:
    """(current AP, AP to switch to). None = already on a usable channel."""
    current = next((a for a in aps if a.active), None)
    if current is None:
        raise TransferError("no active Wi-Fi connection")
    if current.freq in AIRDROP_FREQS:
        return current, None
    usable = [a for a in aps if a.ssid == current.ssid and a.freq in AIRDROP_FREQS]
    if not usable:
        raise TransferError(
            f"Wi-Fi “{current.ssid}” runs on {current.freq} MHz and is not available on any of "
            f"AirDrop's channels 6, 44 or 149. Enable one of them on the router.")
    return current, max(usable, key=lambda a: a.signal)


class WifiPin:
    def __init__(self, run: Runner = _run):
        self.run = run
        self.original: str | None = None     # UUID of the profile to go back to
        self.device: str | None = None

    async def _nmcli(self, *args: str, timeout: float = 30) -> str:
        code, out = await self.run(["nmcli", *args], timeout)
        if code != 0:
            raise TransferError(f"nmcli {' '.join(args[:3])}: {out.strip()}")
        return out

    async def prepare(self, minutes: int) -> str | None:
        """Switch if needed; returns a message for the user, None if nothing changed."""
        if not shutil.which("nmcli") and self.run is _run:
            log.warning("nmcli not found (no NetworkManager): channel left as it is")
            return None
        scan = await self._nmcli("-t", "-f", "ACTIVE,SSID,BSSID,FREQ,SIGNAL",
                                 "dev", "wifi", "list", "--rescan", "auto")
        current, target = choose(parse_scan(scan))
        if target is None:
            return None
        active = await self._nmcli("-t", "-f", "UUID,TYPE,DEVICE", "connection", "show", "--active")
        for line in active.splitlines():
            uuid, kind, dev = (split_terse(line) + ["", "", ""])[:3]
            if kind == "802-11-wireless":
                self.original, self.device = uuid, dev
                break
        if not self.original:
            raise TransferError("active Wi-Fi profile not found")
        await self._delete_temp()
        # Safety net first: restores even if multidrop is killed.
        await self.run(["systemctl", "--user", "stop", f"{RESTORE_UNIT}.timer"], 10)
        await self.run(["systemctl", "--user", "reset-failed", f"{RESTORE_UNIT}.service"], 10)
        code, out = await self.run(
            ["systemd-run", "--user", f"--unit={RESTORE_UNIT}", f"--on-active={minutes + 3}m",
             "/bin/sh", "-c", self.restore_script()], 15)
        if code != 0:
            raise TransferError(f"could not create the safety timer: {out.strip()}")
        channel = AIRDROP_FREQS[target.freq]
        band = "bg" if target.freq < 3000 else "a"
        try:
            await self._nmcli("connection", "clone", self.original, TEMP_PROFILE)
            await self._nmcli("connection", "modify", TEMP_PROFILE,
                              "connection.autoconnect", "no",
                              "802-11-wireless.band", band,
                              "802-11-wireless.channel", str(channel),
                              "802-11-wireless.bssid", target.bssid)
            await self._nmcli("--wait", "45", "connection", "up", TEMP_PROFILE,
                              "ifname", self.device, timeout=60)
        except TransferError:
            await self.restore()
            raise
        log.info("Wi-Fi moved from %d MHz to channel %d (%s)", current.freq, channel, target.bssid)
        return f"Wi-Fi temporarily on channel {channel}"

    def restore_script(self) -> str:
        orig = shlex.quote(self.original or "")
        return (f"nmcli --wait 45 connection up uuid {orig}; "
                f"nmcli connection delete id {TEMP_PROFILE} >/dev/null 2>&1; true")

    async def _delete_temp(self) -> None:
        await self.run(["nmcli", "connection", "delete", "id", TEMP_PROFILE], 20)

    async def restore(self) -> None:
        if self.original is None:
            return
        code, out = await self.run(["nmcli", "--wait", "45", "connection", "up",
                                    "uuid", self.original], 60)
        if code != 0:
            log.warning("restoring Wi-Fi profile failed: %s", out.strip())
        await self._delete_temp()
        if code == 0:
            await self.run(["systemctl", "--user", "stop", f"{RESTORE_UNIT}.timer"], 10)
        self.original = None
