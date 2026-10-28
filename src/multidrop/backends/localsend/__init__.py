"""LocalSend backend (protocol v2): discovery, receiving and sending."""

from __future__ import annotations

import asyncio
import errno
import logging
import secrets
from pathlib import Path

import aiohttp

from multidrop.config import Config
from multidrop.core import (Backend, Device, EventHandler, OfferHandler,
                            _noop_event)

from .client import ClientCertConnector, fetch_info, send_files
from .discovery import MulticastDiscovery
from .identity import Identity
from .protocol import API, base_url, device_from_info, device_info
from .server import LocalSendServer

log = logging.getLogger(__name__)

ANNOUNCE_DELAYS = (0.0, 0.5, 2.0)


def _in_default_lan(address: str) -> bool:
    """Same /24 as the default-route address (good enough for home LANs)."""
    from multidrop.backends.quickshare.mdns import default_address
    return address.rsplit(".", 1)[0] == default_address().rsplit(".", 1)[0]


class LocalSendBackend(Backend):
    name = "localsend"
    partial_accept = "native"

    def __init__(self, config: Config, identity: Identity | None = None):
        self.config = config
        self.ls = config.localsend
        if self.ls.https:
            self.identity = identity or Identity.load_or_create(
                config.state_dir / "localsend")
            self.fingerprint = self.identity.fingerprint
        else:
            self.identity = None
            self.fingerprint = secrets.token_hex(16)
        self.peers: dict[str, Device] = {}
        self.server: LocalSendServer | None = None
        self.discovery: MulticastDiscovery | None = None
        self._http: aiohttp.ClientSession | None = None
        self._tasks: set[asyncio.Task] = set()

    # -- lifecycle --------------------------------------------------------

    @property
    def port(self) -> int:
        return self.server.port if self.server else self.ls.port

    def info(self) -> dict:
        return device_info(self.config.alias, self.fingerprint, self.port,
                           self.ls.https)

    @property
    def started(self) -> bool:
        return self.server is not None

    async def start(self, on_offer: OfferHandler | None = None,
                    on_event: EventHandler = _noop_event, *,
                    allow_other_port: bool = False) -> None:
        """Start the server and discovery.

        Without `on_offer` every incoming transfer is refused; this is the
        mode used for `devices` and `send`, where we only need to be
        reachable for discovery. Then `allow_other_port` lets us fall back
        to a free port if a receiver already holds the configured one.
        """
        ssl_ctx = self.identity.server_ssl_context() if self.identity else None
        if on_offer is not None:
            self.config.download_dir.mkdir(parents=True, exist_ok=True)
        server = LocalSendServer(
            info=self.info, download_dir=self.config.download_dir,
            host=self.ls.bind, port=self.ls.port, ssl_context=ssl_ctx,
            pin=self.ls.pin, on_offer=on_offer, on_event=on_event,
            on_peer=self._add_peer)
        try:
            await server.start()
        except OSError as e:
            if not (allow_other_port and e.errno == errno.EADDRINUSE):
                raise
            server.port = 0
            await server.start()
        self.server = server
        self.discovery = MulticastDiscovery(
            self.ls.multicast_group, self.ls.multicast_port,
            self.ls.multicast_interface, self._on_multicast)
        await self.discovery.start()
        self._spawn(self._announce_burst())

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self.discovery:
            self.discovery.stop()
            self.discovery = None
        if self.server:
            await self.server.stop()
            self.server = None
        if self._http:
            await self._http.close()
            self._http = None

    # -- discovery --------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            connector = (ClientCertConnector(self.identity.client_ssl_context())
                         if self.identity else None)
            self._http = aiohttp.ClientSession(connector=connector)
        return self._http

    async def _announce_burst(self) -> None:
        for i, delay in enumerate(ANNOUNCE_DELAYS):
            await asyncio.sleep(delay - (ANNOUNCE_DELAYS[i - 1] if i else 0))
            self.announce()

    def announce(self) -> None:
        if self.discovery:
            self.discovery.send({**self.info(), "announce": True,
                                 "announcement": True})

    def _add_peer(self, device: Device) -> None:
        if device.id == self.fingerprint:
            return
        known = self.peers.get(device.id)
        # Peers announce on every interface; keep the address in our LAN
        # rather than e.g. a docker bridge address of the same host.
        if (known is not None and known.address != device.address
                and _in_default_lan(known.address)
                and not _in_default_lan(device.address)):
            device.address = known.address
        self.peers[device.id] = device
        if known is None:
            log.info("found %s", device.label())

    def _on_multicast(self, msg: dict, ip: str) -> None:
        try:
            device = device_from_info(msg, ip)
        except ValueError:
            return
        if device.id == self.fingerprint:
            return
        self._add_peer(device)
        if msg.get("announce") or msg.get("announcement"):
            self._spawn(self._answer(device))

    async def _answer(self, device: Device) -> None:
        """Reply to an announcement: HTTP register, falling back to UDP."""
        try:
            async with self.http.post(f"{base_url(device)}/register",
                                      json=self.info(), ssl=False,
                                      timeout=aiohttp.ClientTimeout(total=3)) as r:
                r.raise_for_status()
                info = await r.json(content_type=None)
                answered = device_from_info(info, device.address, device.port)
                answered.port = device.port
                answered.extra["scheme"] = device.extra["scheme"]
                self._add_peer(answered)
                return
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
        if self.discovery:
            self.discovery.send({**self.info(), "announce": False,
                                 "announcement": False})

    async def discover(self, timeout: float = 3.0) -> list[Device]:
        if not self.started:
            await self.start(allow_other_port=True)
        else:
            self.announce()
        await asyncio.sleep(timeout)
        return sorted(self.peers.values(), key=lambda d: d.name.lower())

    async def resolve(self, address: str, port: int | None = None) -> Device:
        """Look up a peer by address without multicast."""
        return await fetch_info(self.http, address, port or 53317)

    # -- sending ----------------------------------------------------------

    async def send(self, device: Device, paths: list[Path],
                   on_event: EventHandler = _noop_event,
                   pin: str | None = None, text: str | None = None) -> None:
        await send_files(self.http, self.info(), device, paths, on_event, pin, text)


__all__ = ["LocalSendBackend", "Identity", "API"]
