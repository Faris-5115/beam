"""Quick Share backend (Google Nearby Share / Samsung Quick Share), Wi-Fi LAN."""

from __future__ import annotations

import asyncio
import os
import errno
import logging
from pathlib import Path

from multidrop.config import Config
from multidrop.core import (Backend, Device, EventHandler, OfferHandler,
                            _noop_event, collect_files)

from . import frames, inbound, outbound

log = logging.getLogger(__name__)


class QuickShareBackend(Backend):
    name = "quickshare"
    partial_accept = "discard"   # the protocol accepts or rejects the whole offer

    def __init__(self, config: Config):
        self.config = config
        self.qs = config.quickshare
        self.endpoint_id = frames.new_endpoint_id()
        self._server: asyncio.base_events.Server | None = None
        self._publisher = None
        self._watch: asyncio.Task | None = None
        self._ble = None
        self._handlers: set[asyncio.Task] = set()
        self.port = self.qs.port
        # Set in tests to act like a phone that scanned this QR code (qr.QrScan).
        self.qr_scan = None

    @property
    def started(self) -> bool:
        return self._server is not None

    async def start(self, on_offer: OfferHandler | None = None,
                    on_event: EventHandler = _noop_event) -> None:
        async def handle(reader, writer):
            task = asyncio.current_task()
            self._handlers.add(task)
            try:
                await inbound.handle_inbound(
                    reader, writer, download_dir=self.config.download_dir,
                    on_offer=on_offer, on_event=on_event, qr_scan=self.qr_scan)
            finally:
                self._handlers.discard(task)

        try:
            self._server = await asyncio.start_server(handle, self.qs.bind, self.qs.port)
        except OSError as e:
            if e.errno != errno.EADDRINUSE or not self.qs.port:
                raise
            self._server = await asyncio.start_server(handle, self.qs.bind, 0)
            log.warning("Quick Share port %d is in use, using random port %d",
                        self.qs.port, self._server.sockets[0].getsockname()[1])
        self.port = self._server.sockets[0].getsockname()[1]
        if self.qs.mdns:
            addresses = self._mdns_addresses()
            if addresses:
                await self._publish(addresses)
            # Follow the network: no address yet (Wi-Fi still reconnecting, e.g.
            # right after an AirDrop session) or a new one after a switch.
            self._watch = asyncio.ensure_future(self._watch_address())

    def _mdns_addresses(self) -> list[str] | None:
        """Configured addresses, else the default route's; None while there is
        only loopback (publishing there makes us invisible to every phone)."""
        if self.qs.mdns_addresses:
            return list(self.qs.mdns_addresses)
        from .mdns import default_address
        address = default_address()
        return None if address.startswith("127.") else [address]

    async def _publish(self, addresses: list[str]) -> None:
        from .mdns import MdnsPublisher
        info = frames.EndpointInfo(self.config.alias, frames.DEVICE_LAPTOP)
        if self.qr_scan is not None:
            info.qr_data = self.qr_scan.tlv_payload(None)
        publisher = MdnsPublisher(addresses)
        await publisher.start(self.endpoint_id, self.port, info)
        publisher.listen()
        self._publisher = publisher
        log.info("Quick Share announced on %s", ", ".join(addresses))

    async def _watch_address(self, interval: float = 3.0) -> None:
        while True:
            await asyncio.sleep(interval)
            want = self._mdns_addresses()
            have = self._publisher.addresses if self._publisher is not None else None
            if want == have:
                continue
            try:
                if self._publisher is not None:
                    await self._publisher.stop()
                    self._publisher = None
                if want:
                    await self._publish(want)
            except Exception as e:      # retried on the next round
                log.warning("Quick Share announcement on %s failed: %s", want, e)

    async def stop(self) -> None:
        if self._watch is not None:
            self._watch.cancel()
            await asyncio.gather(self._watch, return_exceptions=True)
            self._watch = None
        if self._publisher is not None:
            await self._publisher.stop()
            self._publisher = None
        await self._stop_ble()
        if self._server is not None:
            self._server.close()
            for t in list(self._handlers):
                t.cancel()
            await self._server.wait_closed()
            self._server = None

    async def start_ble(self) -> bool:
        """Start the BLE wake-up advertisement; False if not possible."""
        if self._ble is not None:
            return True
        if os.name == "nt":     # BlueZ over D-Bus: Linux only
            return False
        from .ble import BleAdvertiser, BleError
        ble = BleAdvertiser()
        try:
            await ble.start()
        except (BleError, OSError, ImportError) as e:
            log.warning("BLE wake-up unavailable: %s", e)
            return False
        self._ble = ble
        return True

    async def _stop_ble(self) -> None:
        if self._ble is not None:
            await self._ble.stop()
            self._ble = None

    async def discover(self, timeout: float = 4.0) -> list[Device]:
        if self.qs.ble:
            await self.start_ble()
        from .mdns import browse
        addresses = self._mdns_addresses()
        if addresses is None:           # no network yet
            return []
        found = await browse(timeout, addresses, exclude_endpoint=self.endpoint_id)
        if self._publisher is not None:
            # Plus everything heard in between, e.g. phone answers to the router.
            known = {f.endpoint_id for f in found}
            found += [f for f in await self._publisher.found(self.endpoint_id)
                      if f.endpoint_id not in known]
        return sorted((Device(
            protocol="quickshare", id=f.endpoint_id,
            name=f.info.name or f"hidden device {f.endpoint_id}",
            address=f.address, port=f.port,
            device_type=frames.DEVICE_TYPE_NAMES.get(f.info.device_type))
            for f in found), key=lambda d: d.name.lower())

    async def resolve(self, address: str, port: int | None) -> Device:
        if not port:
            port = frames.DEFAULT_PORT   # another multidrop; phones use random ports
        return Device(protocol="quickshare", id=f"{address}:{port}",
                      name=f"{address}:{port}", address=address, port=port)

    async def send(self, device: Device, paths: list[Path],
                   on_event: EventHandler = _noop_event, text: str | None = None) -> None:
        entries = collect_files(paths) if paths else []
        await outbound.send(device.address, device.port, our_name=self.config.alias,
                            entries=entries, text=text, on_event=on_event,
                            target_name=device.name)

    async def send_qr(self, paths: list[Path], on_event: EventHandler = _noop_event,
                      text: str | None = None, timeout: float = 120.0) -> Device:
        """Send to whoever scans our QR code (no BLE needed, no accept prompt).

        Emits "qr" with the URL; the phone scans it, starts advertising over
        mDNS with a token derived from the code, and we connect to it.
        """
        from multidrop.core import TransferError
        from .mdns import browse_until
        from .qr import QrSender
        entries = collect_files(paths) if paths else []
        qr = QrSender()
        on_event("qr", {"url": qr.url})
        found = await browse_until(lambda f: qr.match(f.info) is not None, timeout,
                                   self.qs.mdns_addresses or None,
                                   exclude_endpoint=self.endpoint_id)
        if found is None:
            raise TransferError(f"nobody scanned the QR code within {timeout:.0f} s")
        device = Device(protocol="quickshare", id=found.endpoint_id,
                        name=qr.match(found.info), address=found.address, port=found.port,
                        device_type=frames.DEVICE_TYPE_NAMES.get(found.info.device_type))
        on_event("qr_scanned", {"device": device.name})
        await outbound.send(device.address, device.port, our_name=self.config.alias,
                            entries=entries, text=text, on_event=on_event,
                            target_name=device.name, qr=qr)
        return device


__all__ = ["QuickShareBackend"]
