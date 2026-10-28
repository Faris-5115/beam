"""BLE AirDrop advertisement that wakes nearby Apple receivers before sending.

iPhones (and Macs) only publish `_airdrop._tcp` on AWDL once they see a
sender's Apple Continuity "AirDrop" advertisement (omdrop measured ~1 s
after it appears). Without it a Linux sender usually finds nobody; opendrop
has no BLE part, which is why Apple receivers rarely show up there.

On air: manufacturer data, company 0x004C (Apple), value = Continuity TLV
type 0x05, length 18. Two body shapes are known:

* "os26" (current Apple devices, observed by omdrop):
  40 <3 tag bytes> 00 00 00 00 03 <4 x 2-byte hashes> 00
* "published" (Celosia & Cunche, "Discontinued Privacy", PETS 2020):
  00 x 8, 01, <4 x 2-byte hashes>, 00

The hashes are truncated SHA-256 of the sender's Apple ID, phone number and
e-mail addresses. We have no Apple identity, so they are zero: that matches
no contact, which a receiver set to "Everyone" accepts ("Contacts Only"
cannot work). The tag bytes are zero too.

Own implementation from these published facts (omdrop-awdl's
ble-airdrop-adv.py is GPL-2.0-only and is not used). Runs through BlueZ over
D-Bus as a normal user, adapter settings are never touched.
"""

# No "from __future__ import annotations": dbus-fast reads the D-Bus
# signatures from the annotations at runtime.
import logging

from dbus_fast import PropertyAccess, Variant
from dbus_fast.service import dbus_property

from multidrop.backends.quickshare.ble import Advertisement, BleAdvertiser, BleError

log = logging.getLogger(__name__)

APPLE = 0x004C
AIRDROP_TLV = 0x05
OBJECT_PATH = "/org/multidrop/airdrop/advertisement0"
SHAPES = ("os26", "published")


def payload(shape: str = "os26", hashes: bytes = bytes(8), tag: bytes = bytes(3)) -> bytes:
    """Manufacturer data value (without the company id)."""
    if len(hashes) != 8 or len(tag) != 3:
        raise ValueError("hashes: 8 bytes, tag: 3 bytes")
    if shape == "os26":
        body = b"\x40" + tag + bytes(4) + b"\x03" + hashes + b"\x00"
    elif shape == "published":
        body = bytes(8) + b"\x01" + hashes + b"\x00"
    else:
        raise ValueError(f"unknown shape {shape!r}, one of {SHAPES}")
    return bytes([AIRDROP_TLV, len(body)]) + body


class _AirDropAdvertisement(Advertisement):
    def __init__(self, value: bytes, connectable: bool):
        super().__init__(connectable)
        self._value = value

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":  # noqa: N802,F821
        return {APPLE: Variant("ay", self._value)}


def advertiser(shape: str = "os26", connectable: bool = False) -> BleAdvertiser:
    return BleAdvertiser(_AirDropAdvertisement(payload(shape), connectable), OBJECT_PATH)


__all__ = ["advertiser", "payload", "BleError", "SHAPES"]
