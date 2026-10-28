"""`multidrop devices|send|receive|share` through a running multidropd.

The daemon holds the protocol ports, so the CLI only talks to its Unix
socket API: it starts jobs there and prints the daemon's events with the
same wording as in direct mode.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from multidrop.cli import (_choose, _describe_offer, _human, _parse_address, _pick,
                           _print_devices, _Printer)
from multidrop.config import Config
from multidrop.core import Device, TransferError
from multidrop.daemon.client import DaemonClient

_META = ("seq", "time", "event", "protocol", "job")

# Options that configure protocols started by this process; the daemon has
# its own configuration.
_DIRECT_ONLY = {"alias": "--alias", "port": "--port", "multicast_port": "--multicast-port",
                "multicast_interface": "--multicast-interface", "http": "--http",
                "qs_port": "--qs-port", "mdns_address": "--mdns-address", "no_ble": "--no-ble",
                "dir": "--dir", "share_port": "--share-port"}


def _warn_ignored(args) -> None:
    given = [flag for attr, flag in _DIRECT_ONLY.items() if getattr(args, attr, None)]
    if args.command == "receive" and args.pin:
        given.append("--pin")
    if given:
        print(f"warning: multidropd is running and uses its own configuration; ignored: "
              f"{' '.join(given)} (stop the daemon or use --direct with other ports)",
              file=sys.stderr)


def _details(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if k not in _META}


async def run(args, cfg: Config, client: DaemonClient) -> int:
    _warn_ignored(args)
    cmd = {"devices": devices, "send": send, "receive": receive, "share": share}[args.command]
    return await cmd(args, client)


async def _devices(client: DaemonClient, timeout: float, via: str | None) -> list[Device]:
    groups = await client.get(f"/api/devices?refresh=1&timeout={timeout:g}")
    return [Device(t["protocol"], t.get("id", ""), g["name"], t["address"], t["port"],
                   t.get("model"), g.get("type"))
            for g in groups for t in g["targets"] if not via or t["protocol"] == via]


async def devices(args, client: DaemonClient) -> int:
    return _print_devices(args, await _devices(client, args.timeout, args.via))


async def send(args, client: DaemonClient) -> int:
    paths = [Path(p).expanduser().resolve() for p in args.files]
    if not paths and args.text is None:
        raise TransferError("nothing to send: give files or --text")
    for p in paths:
        if not p.exists():
            raise TransferError(f"not a file: {p}")
    if args.qr:
        if args.to or args.via == "localsend":
            raise TransferError("--qr is Quick Share only and finds the receiver itself")
        target, label = {"qr": True}, "the phone that scans the QR code"
    elif addr := (_parse_address(args.to) if args.to else None):
        host, port = addr
        target = {"address": host, "port": port, "protocol": args.via}
        label = f"{args.to}" + (f" [{args.via}]" if args.via else "")
    else:
        prefer = (await client.get("/api/status"))["prefer"]
        device = _pick(await _devices(client, args.timeout, args.via), args.to, prefer)
        target, label = {"key": f"{device.protocol}:{device.id}"}, device.label()
    since = await client.last_seq()
    job = (await client.post("/api/send", {"target": target, "paths": [str(p) for p in paths],
                                           "text": args.text, "pin": args.pin}))["job"]
    print(f"sending to {label} (via multidropd)", flush=True)
    try:
        return await _follow_job(client, job, since, args)
    except asyncio.CancelledError:          # Ctrl+C: stop the job in the daemon too
        try:
            await client.post(f"/api/jobs/{job}/cancel")
        except TransferError:
            pass
        raise


async def _follow_job(client: DaemonClient, job: str, since: int, args) -> int:
    printer = _Printer()
    async for ev in client.events(since):
        if ev.get("job") != job:
            continue
        name = ev["event"]
        if name == "job_done":
            return 0
        if name == "job_failed":
            raise TransferError(ev.get("error") or "failed")
        if name == "job_cancelled":
            raise TransferError("cancelled")
        if name == "qr":
            from multidrop.backends.quickshare.qr import terminal_qr
            print(terminal_qr(ev["url"]), flush=True)
            print(f"Scan with the phone camera (Android: Quick Share, Samsung: QR scanner)\n"
                  f"{ev['url']}", flush=True)
        elif name == "qr_scanned":
            print(f"scanned by {ev.get('device')}", flush=True)
        else:
            printer(name, _details(ev))
    raise TransferError("multidropd stopped")


async def receive(args, client: DaemonClient) -> int:
    if args.accept == "ask" and not sys.stdin.isatty():
        print("stdin is not a terminal; use --accept all to accept without asking",
              file=sys.stderr)
        return 2
    status = await client.get("/api/status")
    protocols = [p for p in status["protocols"] if not args.via or p == args.via]
    print(f"multidropd is running: receiving through it as '{status['alias']}' "
          f"({', '.join(protocols) or 'no matching protocol'})", flush=True)
    if status["accept"] == "all":
        print("note: multidropd accepts everything itself (daemon.accept = \"all\")", flush=True)
    print(f"saving to {status['download_dir']}", flush=True)

    since = await client.last_seq()
    queue: asyncio.Queue[dict] = asyncio.Queue()
    settled: set[str] = set()
    for offer in await client.get("/api/offers"):
        if not args.via or offer["protocol"] == args.via:
            queue.put_nowait({**offer, "_early": True})
    answering = asyncio.ensure_future(_answer_offers(client, queue, settled, args.accept))
    printer = _Printer()
    try:
        async for ev in client.events(since):
            if "job" in ev or (args.via and ev.get("protocol") not in (None, args.via)):
                continue                # our own sends / other protocols
            name = ev["event"]
            if name == "offer":
                queue.put_nowait(ev["offer"])
            elif name in ("offer_accepted", "offer_rejected", "offer_expired",
                          "offer_withdrawn"):
                oid = ev.get("id") or ev.get("offer", {}).get("id")
                settled.add(oid)
                if name in ("offer_expired", "offer_withdrawn"):
                    print(f"{name.replace('_', ' ')}", flush=True)
            else:
                printer(name, _details(ev))
            if args.once and name in ("session_done", "message", "cancelled"):
                return 0
        print("multidropd stopped", file=sys.stderr)
        return 1
    finally:
        answering.cancel()


async def _answer_offers(client: DaemonClient, queue: asyncio.Queue, settled: set[str],
                         accept: str) -> None:
    """One question at a time; offers answered elsewhere (web UI,
    notification) in the meantime are skipped."""
    while True:
        offer = await queue.get()
        if offer["id"] in settled:
            continue
        files = [(f["name"], f["size"], f.get("text")) for f in offer["files"]]
        if accept == "all":
            print(f"accepting {len(files)} item(s) from {offer['from']} "
                  f"({offer['address']}, {offer['protocol']})", flush=True)
            chosen = list(range(len(files)))
        else:
            _describe_offer(offer["from"], offer["address"], offer["protocol"], files)
            if offer.get("pin") and offer.get("_early"):   # else printed by the pin event
                print(f"Quick Share code {offer['pin']}: must match the code on the other device")
            chosen = await asyncio.to_thread(
                _choose, [n if t is None else "[text]" for n, _, t in files],
                offer.get("partial", "none"))
        if offer["id"] in settled:
            print("(already answered elsewhere)", flush=True)
            continue
        body = {"accept": bool(chosen)}
        if chosen and len(chosen) < len(files):
            body["files"] = [offer["files"][i]["id"] for i in chosen]
        try:
            await client.post(f"/api/offers/{offer['id']}", body)
        except TransferError as e:
            print(f"could not answer: {e}", flush=True)


async def share(args, client: DaemonClient) -> int:
    from multidrop.backends.quickshare.qr import terminal_qr
    paths = [Path(p).expanduser().resolve() for p in args.files]
    result = await client.post("/api/share", {
        "paths": [str(p) for p in paths], "pin": args.pin or "", "no_pin": args.no_pin,
        "minutes": args.minutes, "address": args.address})
    since = await client.last_seq()
    print(terminal_qr(result["url"]), flush=True)
    print(f"Open on the phone (same Wi-Fi): {result['url']}", flush=True)
    if result.get("pin"):
        print(f"PIN: {result['pin']}", flush=True)
    print(f"{len(result['files'])} file(s), {_human(result['size'])}; stops in "
          f"{args.minutes:g} min or with Ctrl+C (shared by multidropd)", flush=True)
    try:
        async for ev in client.events(since):
            name = ev["event"]
            if name == "share_download":
                print(f"download by {ev['from']}: {ev['file']}", flush=True)
            elif name == "share_visit":
                print(f"opened by {ev['from']}", flush=True)
            elif name == "share_wrong_pin":
                print(f"wrong PIN from {ev['from']}", flush=True)
            elif name == "share_stopped":
                print(f"share stopped ({ev['reason']}, {ev['downloads']} download(s))",
                      flush=True)
                return 0
        return 1
    except asyncio.CancelledError:
        try:
            await client.delete("/api/share")
        except TransferError:
            pass
        raise
