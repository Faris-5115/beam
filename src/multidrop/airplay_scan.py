"""What AirPlay receivers on the network can do, from their own mDNS records.

Every receiver announces `_airplay._tcp` with a TXT `features` bitmask
("0xLOW,0xHIGH"). Bit 7 is screen mirroring, bit 9 audio (as documented by
openairplay/UxPlay). The desktop app uses this instead of guessing from model
names which receivers can show a screen (Sonos speakers cannot).
"""

from __future__ import annotations

import asyncio

from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from multidrop.backends.quickshare.mdns import _zeroconf, default_address

SERVICE = "_airplay._tcp.local."
FEATURE_SCREEN = 1 << 7
FEATURE_AUDIO = 1 << 9


def parse_features(value: str) -> int | None:
    """'0x5A7FFFF7,0x1E' -> 64-bit mask (high word after the comma)."""
    try:
        parts = [int(p, 16) for p in value.split(",")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 2:
        return None
    return parts[0] | (parts[1] << 32 if len(parts) == 2 else 0)


def describe(txt: dict[bytes, bytes | None]) -> dict:
    def text(key: bytes) -> str:
        raw = txt.get(key)
        return raw.decode(errors="replace") if raw else ""
    features = parse_features(text(b"features") or text(b"ft"))
    return {"device_id": text(b"deviceid").upper(), "model": text(b"model"),
            "screen": bool(features & FEATURE_SCREEN) if features is not None else None,
            "audio": bool(features & FEATURE_AUDIO) if features is not None else None}


async def scan(timeout: float = 3.0, addresses: list[str] | None = None) -> list[dict]:
    zc = _zeroconf(addresses or [default_address()])
    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    browser = AsyncServiceBrowser(zc.zeroconf, SERVICE, handlers=[on_change])
    out = []
    try:
        await asyncio.sleep(timeout)

        async def one(name: str) -> dict | None:
            info = AsyncServiceInfo(SERVICE, name)
            if not await info.async_request(zc.zeroconf, 2000):
                return None
            d = describe(info.properties or {})
            d["name"] = name[: -len(SERVICE) - 1]
            d["ip"] = next(iter(info.parsed_addresses(IPVersion.V4Only)), "")
            return d
        out = [d for d in await asyncio.gather(*(one(n) for n in names)) if d]
    finally:
        await browser.async_cancel()
        await zc.async_close()
    return sorted(out, key=lambda d: d["name"].lower())
