"""Desktop notifications over org.freedesktop.Notifications (session D-Bus).

Incoming offers get "Accept"/"Decline" buttons; clicking the notification
itself opens the web UI. Everything else (received, sent, failed) is
informational. Works with any notification server that supports actions
(GNOME, KDE, dunst, mako, ...); without action support the web UI decides.
"""

from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Callable

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

log = logging.getLogger(__name__)

IFACE = "org.freedesktop.Notifications"
PATH = "/org/freedesktop/Notifications"
URGENCY_NORMAL, URGENCY_CRITICAL = 1, 2


def _human(n: int | None) -> str:
    if n is None:
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return str(n)


class Notifier:
    def __init__(self, answer: Callable[[str, bool], bool], open_ui: Callable[[], None],
                 open_folder: Callable[[], None], bus_address: str | None = None):
        self._answer = answer
        self._open_ui = open_ui
        self._open_folder = open_folder
        self._bus_address = bus_address
        self._bus: MessageBus | None = None
        self.actions = False
        self._offer_by_note: dict[int, str] = {}
        self._note_by_offer: dict[str, int] = {}
        self._folder_notes: set[int] = set()
        self._decided: set[str] = set()   # offers decided before their note existed
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._bus = await MessageBus(bus_address=self._bus_address,
                                     bus_type=BusType.SESSION).connect()
        await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                         "org.freedesktop.DBus", "AddMatch", "s",
                         [f"type='signal',interface='{IFACE}'"])
        self._bus.add_message_handler(self._on_message)
        caps = await self._call(IFACE, PATH, IFACE, "GetCapabilities", "", [])
        self.actions = "actions" in (caps[0] if caps else [])
        log.info("notifications ready (actions %s)", "on" if self.actions else "off")

    async def stop(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def _call(self, dest, path, iface, member, signature, body) -> list | None:
        reply = await self._bus.call(Message(destination=dest, path=path, interface=iface,
                                             member=member, signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            log.warning("D-Bus %s.%s: %s %s", iface, member, reply.error_name, reply.body)
            return None
        return reply.body

    async def notify(self, summary: str, body: str = "", actions: list[str] | None = None,
                     urgency: int = URGENCY_NORMAL, timeout: int = -1) -> int:
        if self._bus is None:
            return 0
        reply = await self._call(IFACE, PATH, IFACE, "Notify", "susssasa{sv}i", [
            "multidrop", 0, "folder-download", summary, html.escape(body),
            actions or [], {"urgency": Variant("y", urgency)}, timeout])
        return reply[0] if reply else 0

    async def close(self, note_id: int) -> None:
        if self._bus is not None and note_id:
            await self._call(IFACE, PATH, IFACE, "CloseNotification", "u", [note_id])

    # -- daemon events ----------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_failure)

    def on_event(self, item: dict) -> None:
        """Called synchronously for every daemon event."""
        if self._bus is None:
            return
        event = item["event"]
        if event == "offer":
            self._spawn(self._offer(item["offer"]))
        elif event in ("offer_accepted", "offer_rejected", "offer_expired", "offer_withdrawn"):
            offer_id = item.get("id") or item.get("offer", {}).get("id")
            note = self._note_by_offer.pop(offer_id, 0)
            self._offer_by_note.pop(note, None)
            if note:
                self._spawn(self.close(note))
            else:
                self._decided.add(offer_id)
        elif event == "session_done":
            self._spawn(self._received(item))
        elif event == "message":
            text = item.get("text", "")
            self._spawn(self.notify(f"Message from {item.get('from', '?')}",
                                    text[:300] + ("…" if len(text) > 300 else "")))
        elif event == "job_done":
            info = item["info"]
            self._spawn(self.notify(f"Sent to {info['to']}",
                                    ", ".join(info["items"][:5])))
        elif event == "job_failed":
            self._spawn(self.notify(f"Sending to {item['info']['to']} failed",
                                    item.get("error", ""), urgency=URGENCY_CRITICAL))
        elif event in ("failed", "cancelled") and "job" not in item:
            self._spawn(self.notify(f"Receiving from {item.get('from', '?')} was cancelled",
                                    item.get("error", "")))

    async def _offer(self, offer: dict) -> None:
        files = offer["files"]
        lines = [f"[Text] {f['text'][:80]}" if f["text"] is not None
                 else f"{f['name']} ({_human(f['size'])})" for f in files[:5]]
        if len(files) > 5:
            lines.append(f"… and {len(files) - 5} more")
        if offer.get("pin"):
            lines.append(f"Code: {offer['pin']} (must match the sender)")
        actions = ["accept", "Accept", "reject", "Decline", "default", "Show"]
        note = await self.notify(
            f"{offer['from']} wants to send {len(files)} item(s) ({offer['protocol']})",
            "\n".join(lines), actions if self.actions else None,
            urgency=URGENCY_CRITICAL, timeout=0)
        if offer["id"] in self._decided:
            self._decided.discard(offer["id"])
            await self.close(note)
        elif note:
            self._offer_by_note[note] = offer["id"]
            self._note_by_offer[offer["id"]] = note

    async def _received(self, item: dict) -> None:
        note = await self.notify(f"Received from {item.get('from', '?')}",
                                 "Files saved",
                                 ["default", "Open folder"] if self.actions else None)
        if note:
            self._folder_notes.add(note)

    def _on_message(self, msg: Message) -> bool:
        if msg.message_type != MessageType.SIGNAL or msg.interface != IFACE:
            return False
        if msg.member == "ActionInvoked":
            note, key = msg.body
            offer_id = self._offer_by_note.get(note)
            if offer_id is not None and key in ("accept", "reject"):
                self._answer(offer_id, key == "accept")
            elif note in self._folder_notes:
                self._open_folder()
            elif offer_id is not None or key == "default":
                self._open_ui()
        elif msg.member == "NotificationClosed":
            note = msg.body[0]
            # Closing an offer notification does not decide; the web UI still can.
            offer_id = self._offer_by_note.pop(note, None)
            self._note_by_offer.pop(offer_id, None)
            self._folder_notes.discard(note)
        return False


def _log_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("notification failed: %s", task.exception())
