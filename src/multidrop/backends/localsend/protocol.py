"""LocalSend protocol v2 constants and message helpers.

Spec: https://github.com/localsend/protocol (v2.2).
"""

from __future__ import annotations

from multidrop.core import Device

PROTOCOL_VERSION = "2.2"
API = "/api/localsend/v2"


def device_info(alias: str, fingerprint: str, port: int, https: bool) -> dict:
    """The info object used in announcements, register and prepare-upload."""
    return {
        "alias": alias,
        "version": PROTOCOL_VERSION,
        "deviceModel": "Linux",
        "deviceType": "desktop",
        "fingerprint": fingerprint,
        "port": port,
        "protocol": "https" if https else "http",
        "download": False,
    }


def device_from_info(info: dict, address: str,
                     default_port: int = 53317) -> Device:
    """Build a Device from a peer's info object. Raises ValueError if invalid."""
    if not isinstance(info, dict):
        raise ValueError("info is not an object")
    fingerprint = info.get("fingerprint")
    alias = info.get("alias")
    if not isinstance(fingerprint, str) or not isinstance(alias, str):
        raise ValueError("missing alias or fingerprint")
    port = info.get("port", default_port)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError("bad port")
    protocol = info.get("protocol", "https")
    if protocol not in ("http", "https"):
        protocol = "https"
    return Device(
        protocol="localsend",
        id=fingerprint,
        name=alias,
        address=address,
        port=port,
        model=info.get("deviceModel"),
        device_type=info.get("deviceType"),
        extra={"scheme": protocol, "version": info.get("version")},
    )


def base_url(device: Device) -> str:
    host = device.address
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    return f"{device.extra.get('scheme', 'https')}://{host}:{device.port}{API}"
