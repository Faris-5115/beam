"""A minimal org.freedesktop.Notifications server for tests (private bus)."""

# No "from __future__ import annotations": dbus-fast reads the signatures.
from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, method, signal


class _Server(ServiceInterface):
    def __init__(self, owner):
        super().__init__("org.freedesktop.Notifications")
        self.owner = owner

    @method()
    def GetCapabilities(self) -> "as":  # noqa: N802,F821
        return ["actions", "body"]

    @method()
    def Notify(self, app: "s", replaces: "u", icon: "s", summary: "s", body: "s",  # noqa: N802,F821
               actions: "as", hints: "a{sv}", timeout: "i") -> "u":  # noqa: F821
        self.owner.notes.append((len(self.owner.notes) + 1, summary, body, actions))
        return len(self.owner.notes)

    @method()
    def CloseNotification(self, nid: "u"):  # noqa: N802,F821
        self.owner.closed.append(nid)
        self.NotificationClosed(nid, 3)

    @method()
    def GetServerInformation(self) -> "ssss":  # noqa: N802,F821
        return ["fake", "multidrop-tests", "1", "1.2"]

    @signal()
    def ActionInvoked(self, nid, key) -> "us":  # noqa: N802,F821
        return [nid, key]

    @signal()
    def NotificationClosed(self, nid, reason) -> "uu":  # noqa: N802,F821
        return [nid, reason]


class FakeNotifications:
    def __init__(self):
        self.notes = []      # (id, summary, body, actions)
        self.closed = []
        self._bus = None
        self._server = None

    @classmethod
    async def start(cls, address):
        self = cls()
        self._bus = await MessageBus(bus_address=address, bus_type=BusType.SESSION).connect()
        self._server = _Server(self)
        self._bus.export("/org/freedesktop/Notifications", self._server)
        await self._bus.request_name("org.freedesktop.Notifications")
        return self

    async def invoke(self, nid, key):
        self._server.ActionInvoked(nid, key)

    async def stop(self):
        self._bus.disconnect()
