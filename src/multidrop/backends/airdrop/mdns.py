"""`_airdrop._tcp` over mDNS (on awdl0 in real use, on lo in tests).

Instance name = 12 hex digits (our service id), TXT "flags" = receiver
capabilities (opendrop config.py, from sharingd).

Announcement bursts: iPhones only listen on AWDL in short slots, so a
registration (announced a few times, then only answers to queries) is easily
missed. While visible, the Publisher repeats the unsolicited announcement
(PTR, SRV, TXT, AAAA) in bursts: `burst` copies 50 ms apart every `interval`
seconds. Values from omdrop-owl's omdrop-discoverable (3 copies every 3 s in
shared mode, where OWL sends multicast in AWDL's multicast slots). omdrop-owl
does this with omdrop-awdl's awdl-airdrop-adv.py, a raw-socket (root) tool
under GPL-2.0-only; here it is our own code on python-zeroconf's sockets
(UDP 5353, hop limit 255), so it runs as a normal user. OWL adds the AWDL
encapsulation itself (that is why omdrop-owl passes --plain).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass

from zeroconf import DNSOutgoing, IPVersion, ServiceStateChange, const
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

log = logging.getLogger(__name__)

SERVICE_TYPE = "_airdrop._tcp.local."
FLAG_URL, FLAG_DVZIP, FLAG_MIXED_TYPES, FLAG_DISCOVER = 0x01, 0x02, 0x08, 0x80
FLAGS = FLAG_URL | FLAG_DVZIP | FLAG_MIXED_TYPES | FLAG_DISCOVER


def _version(addresses: list[str]) -> IPVersion:
    v6 = {ipaddress.ip_address(a.split("%")[0]).version == 6 for a in addresses}
    return IPVersion.V6Only if v6 == {True} else IPVersion.V4Only if v6 == {False} \
        else IPVersion.All


def _zc(addresses: list[str]) -> AsyncZeroconf:
    return AsyncZeroconf(interfaces=[a.split("%")[0] for a in addresses],
                         ip_version=_version(addresses))


BURST_GAP = 0.05


class Publisher:
    def __init__(self, addresses: list[str], interval: float = 3.0, burst: int = 3,
                 flags: int = FLAGS):
        self.addresses = addresses
        self.interval = interval      # 0 = no bursts, only the registration
        self.burst = burst
        self.flags = flags
        self.announcements = 0        # packets sent by the burst loop
        self._zc: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None
        self._task: asyncio.Task | None = None

    async def start(self, service_id: str, port: int) -> None:
        self._info = AsyncServiceInfo(
            SERVICE_TYPE, f"{service_id}.{SERVICE_TYPE}", port=port,
            properties={"flags": str(self.flags)},
            parsed_addresses=[a.split("%")[0] for a in self.addresses],
            server=f"multidrop-{service_id}.local.")
        self._zc = _zc(self.addresses)
        await self._zc.async_register_service(self._info)
        if self.interval > 0 and self.burst > 0:
            self._task = asyncio.ensure_future(self._bursts())

    def announcement(self) -> DNSOutgoing:
        """Unsolicited authoritative response with all our records."""
        info = self._info
        out = DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA)
        for record in (info.dns_pointer(), info.dns_service(), info.dns_text(),
                       *info.dns_addresses()):
            out.add_answer_at_time(record, 0)
        return out

    async def _bursts(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            for i in range(self.burst):
                try:
                    self._zc.zeroconf.async_send(self.announcement())
                    self.announcements += 1
                except Exception as e:     # keep announcing; log once per burst
                    log.debug("announcement failed: %s", e)
                if i + 1 < self.burst:
                    await asyncio.sleep(BURST_GAP)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._zc is not None:
            try:
                await self._zc.async_unregister_service(self._info)
            finally:
                await self._zc.async_close()
                self._zc = None


@dataclass
class Found:
    service_id: str
    address: str      # with %scope for IPv6 link-local
    port: int
    flags: int


async def browse(timeout: float, addresses: list[str], scope: str | None = None,
                 exclude: str | None = None) -> list[Found]:
    zc = _zc(addresses)
    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    browser = AsyncServiceBrowser(zc.zeroconf, SERVICE_TYPE, handlers=[on_change])
    found = []
    try:
        await asyncio.sleep(timeout)
        for name in names:
            sid = name[: -len(SERVICE_TYPE) - 1]
            if sid == exclude:
                continue
            info = AsyncServiceInfo(SERVICE_TYPE, name)
            if not await info.async_request(zc.zeroconf, 3000) or not info.port:
                continue
            addrs = info.parsed_addresses()
            if not addrs:
                continue
            addr = addrs[0]
            if scope and addr.startswith("fe80:"):
                addr = f"{addr}%{scope}"
            try:
                flags = int((info.properties or {}).get(b"flags") or 0)
            except ValueError:
                flags = 0
            found.append(Found(sid, addr, info.port, flags))
    finally:
        await browser.async_cancel()
        await zc.async_close()
    return found
