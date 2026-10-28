"""LocalSend multicast discovery (spec section 3.1).

Only listens and sends UDP datagrams on an ordinary socket; it never changes
any interface settings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
from collections.abc import Callable

log = logging.getLogger(__name__)

# (message, sender_ip)
MessageHandler = Callable[[dict, str], None]


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_message: MessageHandler):
        self.on_message = on_message

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(msg, dict):
            self.on_message(msg, addr[0])

    def error_received(self, exc: Exception) -> None:
        log.debug("multicast socket error: %s", exc)


class MulticastDiscovery:
    def __init__(self, group: str, port: int, interface: str,
                 on_message: MessageHandler):
        self.group = group
        self.port = port
        self.interface = interface
        self.on_message = on_message
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.port))
        mreq = struct.pack("4s4s", socket.inet_aton(self.group),
                           socket.inet_aton(self.interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        if self.interface != "0.0.0.0":
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(self.interface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        # Other instances on this host must hear us too; we drop our own
        # datagrams by fingerprint.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Protocol(self.on_message), sock=sock)

    def send(self, msg: dict) -> None:
        if self._transport is None:
            return
        self._transport.sendto(json.dumps(msg).encode(), (self.group, self.port))

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
