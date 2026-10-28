"""BLE "someone nearby is sharing" advertisement, through BlueZ over D-Bus.

Android only publishes its Quick Share mDNS service after it sees this
advertisement (service UUID 0xFE2C, service data prefix fc128e0142 + zeros,
then random bytes; see NearDrop PROTOCOL.md and rquickshare blea.rs).

Uses org.bluez.LEAdvertisingManager1 as a normal user. It registers an
advertisement and unregisters it again; adapter settings (Powered,
Discoverable, Alias, ...) are never touched.
"""

# No "from __future__ import annotations": dbus-fast reads the D-Bus
# signatures from the annotations at runtime.
import logging
import os

from dbus_fast import BusType, PropertyAccess, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method

log = logging.getLogger(__name__)

SERVICE_UUID = "0000fe2c-0000-1000-8000-00805f9b34fb"
OBJECT_PATH = "/org/multidrop/quickshare/advertisement0"


def service_data() -> bytes:
    return bytes.fromhex("fc128e0142") + bytes(9) + os.urandom(10)


class Advertisement(ServiceInterface):
    """org.bluez.LEAdvertisement1 base; subclasses add the payload property."""

    def __init__(self, connectable: bool = False):
        super().__init__("org.bluez.LEAdvertisement1")
        self._type = "peripheral" if connectable else "broadcast"

    @method()
    def Release(self):  # noqa: N802 (D-Bus name)
        log.debug("advertisement released by BlueZ")

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":  # noqa: N802,F821
        return self._type


class _Advertisement(Advertisement):
    def __init__(self, data: bytes):
        super().__init__()
        self._data = data

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self) -> "a{sv}":  # noqa: N802,F821
        return {SERVICE_UUID: Variant("ay", self._data)}


class BleError(Exception):
    pass


class BleAdvertiser:
    """Registers one advertisement (default: Quick Share's FE2C wake-up)."""

    def __init__(self, advertisement: Advertisement | None = None, path: str = OBJECT_PATH):
        self._advertisement = advertisement
        self._path = path
        self._bus = None
        self._manager = None

    async def start(self) -> str:
        """Register the advertisement; returns the adapter path used."""
        try:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            adapter = await self._find_adapter()
            self._bus.export(self._path, self._advertisement or _Advertisement(service_data()))
            intro = await self._bus.introspect("org.bluez", adapter)
            obj = self._bus.get_proxy_object("org.bluez", adapter, intro)
            self._manager = obj.get_interface("org.bluez.LEAdvertisingManager1")
            await self._manager.call_register_advertisement(self._path, {})
            return adapter
        except Exception as e:
            await self.stop()
            raise BleError(f"BLE advertising unavailable: {e}") from e

    async def _find_adapter(self) -> str:
        intro = await self._bus.introspect("org.bluez", "/")
        root = self._bus.get_proxy_object("org.bluez", "/", intro)
        objects = await root.get_interface(
            "org.freedesktop.DBus.ObjectManager").call_get_managed_objects()
        for path, ifaces in sorted(objects.items()):
            if "org.bluez.LEAdvertisingManager1" in ifaces:
                powered = ifaces.get("org.bluez.Adapter1", {}).get("Powered")
                if powered is None or powered.value:
                    return path
        raise BleError("no powered Bluetooth adapter with LE advertising")

    async def stop(self) -> None:
        if self._bus is None:
            return
        try:
            if self._manager is not None:
                await self._manager.call_unregister_advertisement(self._path)
        except Exception as e:
            log.debug("unregister failed: %s", e)
        finally:
            self._manager = None
            self._bus.unexport(self._path)
            self._bus.disconnect()
            self._bus = None
