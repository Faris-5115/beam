"""mDNS publishing and browsing for `_FC9F5ED42C8A._tcp` via python-zeroconf."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass

from zeroconf import (DNSAddress, DNSIncoming, DNSOutgoing, DNSPointer, DNSService, DNSText,
                      IPVersion, ServiceStateChange, const)
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from . import frames

log = logging.getLogger(__name__)


def default_address() -> str:
    """IPv4 address of the default-route interface (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never actually contacted
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _zeroconf(addresses: list[str]) -> AsyncZeroconf:
    return AsyncZeroconf(interfaces=addresses, ip_version=IPVersion.V4Only)


class MdnsPublisher:
    def __init__(self, addresses: list[str] | None = None):
        self.addresses = addresses or [default_address()]
        self._zc: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None
        self._unicast: UnicastResponder | None = None
        self._browser: AsyncServiceBrowser | None = None
        self.heard: dict[str, float] = {}       # service name -> last seen (monotonic)

    async def start(self, endpoint_id: str, port: int, info: frames.EndpointInfo) -> None:
        instance = frames.mdns_instance_name(endpoint_id)
        self._info = AsyncServiceInfo(
            frames.SERVICE_TYPE, f"{instance}.{frames.SERVICE_TYPE}",
            port=port, properties={"n": frames.b64url(info.serialize())},
            parsed_addresses=self.addresses,
            server=f"multidrop-{endpoint_id.lower()}.local.")
        self._zc = _zeroconf(self.addresses)
        await self._zc.async_register_service(self._info)
        try:
            self._unicast = await UnicastResponder.start(self._info, self.addresses[0])
        except OSError as e:
            log.warning("unicast mDNS answers unavailable: %s", e)

    def listen(self) -> None:
        """Keep a browser on the long-lived publisher socket. Phones on some
        networks announce themselves only when the router's mDNS gateway
        asks (about once a minute), so a short scan window mostly misses them."""
        if self._zc is None or self._browser is not None:
            return

        def on_change(zeroconf, service_type, name, state_change):
            if state_change is ServiceStateChange.Removed:
                self.heard.pop(name, None)
            else:
                self.heard[name] = time.monotonic()
        self._browser = AsyncServiceBrowser(self._zc.zeroconf, frames.SERVICE_TYPE,
                                            handlers=[on_change])

    async def found(self, exclude_endpoint: str | None, max_age: float = 300) -> list[FoundService]:
        """Services heard recently, resolved from the cache (asks if needed)."""
        if self._zc is None:
            return []
        now = time.monotonic()
        names = [n for n, t in self.heard.items() if now - t <= max_age]
        lo = _is_loopback(self.addresses)
        results = await asyncio.gather(*(_resolve(self._zc, n, exclude_endpoint, lo)
                                         for n in names))
        return [f for f in results if f is not None]

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.async_cancel()
            self._browser = None
        if self._unicast is not None:
            self._unicast.close()
            self._unicast = None
        if self._zc is None:
            return
        try:
            await self._zc.async_unregister_service(self._info)
        finally:
            await self._zc.async_close()
            self._zc = None


class UnicastResponder(asyncio.DatagramProtocol):
    """Answers Quick Share queries a second time, unicast to the asking phone.

    Some access points/routers (mDNS gateways) forward multicast from phones
    but not multicast towards them, except for services they know (AirPlay,
    Chromecast). The phone then asks for `_FC9F5ED42C8A._tcp`, python-zeroconf
    answers by multicast, and the answer never arrives: the phone shows no
    devices. A unicast reply to the querier's port 5353 (RFC 6762 §5.4/§6.7)
    takes the ordinary unicast path, which such networks do pass.
    """

    MIN_INTERVAL = 1.0      # per querier

    def __init__(self, info: AsyncServiceInfo, address: str):
        self.info = info
        self.address = address
        self.transport: asyncio.DatagramTransport | None = None
        self._last: dict[str, float] = {}
        self._announced = 0.0

    @classmethod
    async def start(cls, info: AsyncServiceInfo, address: str) -> "UnicastResponder":
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("", 5353))           # shared with zeroconf, both get the queries
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                        struct.pack("4s4s", socket.inet_aton("224.0.0.251"),
                                    socket.inet_aton(address)))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
        # RFC 6762 §11: responses, unicast ones too, go out with IP TTL 255;
        # receivers may drop others as possibly off-link.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.setblocking(False)
        responder = cls(info, address)
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: responder, sock=sock)
        return responder

    def connection_made(self, transport) -> None:
        self.transport = transport

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()

    def query(self, data: bytes) -> DNSIncoming | None:
        """The query if it asks for our service type or instance."""
        try:
            msg = DNSIncoming(data)
        except Exception:
            return None
        if not msg.is_query():
            return None
        ours = {self.info.type.lower(), self.info.name.lower()}
        return msg if any(q.name.lower() in ours for q in msg.questions) else None

    def response(self, legacy: DNSIncoming | None = None) -> bytes:
        """Our records. `legacy`: answer a one-shot query from a port other
        than 5353 (RFC 6762 §6.7: same id, question repeated, TTL <= 10 s).
        mDNS gateways in routers ask this way on behalf of a phone and drop
        answers that don't match."""
        if legacy is None:
            out = DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA)
            records = [self.info.dns_pointer(), self.info.dns_service(), self.info.dns_text(),
                       *self.info.dns_addresses(version=IPVersion.V4Only)]
        else:
            out = DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA, False, legacy.id)
            for q in legacy.questions:
                out.add_question(q)
            ttl = 10
            info = self.info
            records = [DNSPointer(info.type, const._TYPE_PTR, const._CLASS_IN, ttl, info.name),
                       DNSService(info.name, const._TYPE_SRV, const._CLASS_IN, ttl, 0, 0,
                                  info.port, info.server),
                       DNSText(info.name, const._TYPE_TXT, const._CLASS_IN, ttl, info.text),
                       *(DNSAddress(info.server, const._TYPE_A, const._CLASS_IN, ttl, a)
                         for a in info.addresses_by_version(IPVersion.V4Only))]
        for record in records:
            out.add_answer_at_time(record, 0)
        return out.packets()[0]

    def datagram_received(self, data: bytes, addr) -> None:
        ip, port = addr[0], addr[1]
        if ip == self.address or self.transport is None:
            return
        msg = self.query(data)
        if msg is None:
            return
        if port != 5353:
            # One-shot ("legacy unicast") query, e.g. a router's mDNS gateway
            # asking for a phone: always answer, on the asking port.
            self.transport.sendto(self.response(legacy=msg), (ip, port))
        now = time.monotonic()
        if now - self._last.get(ip, 0) < self.MIN_INTERVAL:
            return
        self._last[ip] = now
        packet = self.response()
        self.transport.sendto(packet, (ip, 5353))
        # Like rquickshare's register_resend: Android may miss a service that
        # was announced before its discovery started, so announce again now.
        if now - self._announced > self.MIN_INTERVAL:
            self._announced = now
            self.transport.sendto(packet, ("224.0.0.251", 5353))
        log.debug("unicast mDNS answer to %s", ip)


@dataclass
class FoundService:
    endpoint_id: str
    address: str
    port: int
    info: frames.EndpointInfo


def _is_loopback(addresses: list[str] | None) -> bool:
    return any(a.startswith("127.") for a in addresses or [default_address()])


async def _resolve(zc: AsyncZeroconf, name: str, exclude_endpoint: str | None,
                   loopback_ok: bool = False) -> FoundService | None:
    instance = name[: -len(frames.SERVICE_TYPE) - 1]
    endpoint_id = frames.parse_mdns_instance_name(instance)
    if endpoint_id is None or endpoint_id == exclude_endpoint:
        return None
    info = AsyncServiceInfo(frames.SERVICE_TYPE, name)
    if not await info.async_request(zc.zeroconf, 3000):
        return None
    # On a real network loopback is never a peer (e.g. a test run's or another
    # local instance's advertisement heard on lo); tests run on 127.0.0.1.
    addrs = [a for a in info.parsed_addresses(IPVersion.V4Only)
             if loopback_ok or not a.startswith("127.")]
    raw_n = (info.properties or {}).get(b"n")
    if not addrs or not info.port or not raw_n:
        return None
    try:
        ep = frames.EndpointInfo.parse(frames.b64url_decode(raw_n.decode()))
    except (ValueError, UnicodeDecodeError):
        return None
    return FoundService(endpoint_id, addrs[0], info.port, ep)


async def browse_until(predicate, timeout: float, addresses: list[str] | None = None,
                       exclude_endpoint: str | None = None) -> FoundService | None:
    """Watch the network until a service satisfies predicate(FoundService)."""
    zc = _zeroconf(addresses or [default_address()])
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            loop.call_soon_threadsafe(queue.put_nowait, name)

    browser = AsyncServiceBrowser(zc.zeroconf, frames.SERVICE_TYPE, handlers=[on_change])
    try:
        async with asyncio.timeout(timeout):
            while True:
                found = await _resolve(zc, await queue.get(), exclude_endpoint,
                                       _is_loopback(addresses))
                if found is not None and predicate(found):
                    return found
    except TimeoutError:
        return None
    finally:
        await browser.async_cancel()
        await zc.async_close()


async def browse(timeout: float, addresses: list[str] | None = None,
                 exclude_endpoint: str | None = None) -> list[FoundService]:
    zc = _zeroconf(addresses or [default_address()])
    names: set[str] = set()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            names.add(name)

    browser = AsyncServiceBrowser(zc.zeroconf, frames.SERVICE_TYPE, handlers=[on_change])
    found: dict[str, FoundService] = {}
    try:
        await asyncio.sleep(timeout)
        for name in list(names):
            f = await _resolve(zc, name, exclude_endpoint, _is_loopback(addresses))
            if f is not None:
                found[f.endpoint_id] = f
    finally:
        await browser.async_cancel()
        await zc.async_close()
    return list(found.values())
