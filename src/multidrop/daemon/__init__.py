"""multidropd: keeps LocalSend and Quick Share running in the background.

Serves the API on a Unix socket and the web UI on 127.0.0.1, asks about
incoming transfers through desktop notifications. Meant to run as a
systemd --user service (data/systemd/multidropd.service).
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
from pathlib import Path

from aiohttp import web

from multidrop.config import WINDOWS, Config, runtime_dir

from .api import WEB_PORT, build_app
from .service import Daemon

log = logging.getLogger("multidropd")


class AlreadyRunning(Exception):
    pass


def load_or_create_token(path: Path) -> str:
    """Stable web UI token (so a bookmark keeps working), mode 0600."""
    try:
        token = path.read_text().strip()
        if len(token) >= 20:
            return token
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def socket_path(cfg: Config) -> Path:
    return Path(cfg.daemon.socket).expanduser() if cfg.daemon.socket \
        else runtime_dir() / "multidropd.sock"


def _check_not_running(sock: Path) -> None:
    if WINDOWS:
        _check_not_running_tcp(sock.parent / "url")
        return
    if not sock.exists():
        return
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(str(sock))
        except OSError:
            sock.unlink()          # stale socket of a crashed daemon
            return
    raise AlreadyRunning(f"multidropd already running ({sock})")


def _check_not_running_tcp(url_file: Path) -> None:
    """Windows has no Unix socket here: a live web UI port means a daemon."""
    try:
        url = url_file.read_text().strip()
        port = int(url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
    except (OSError, ValueError, IndexError):
        return
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        url_file.unlink(missing_ok=True)
        return
    raise AlreadyRunning(f"multidropd already running ({url})")


def _open(target: str) -> None:
    """Open a URL or folder with the desktop's default app, only ever on a
    user's click."""
    try:
        if WINDOWS:
            os.startfile(target)        # noqa: S606 (URL or folder, our own)
            return
        subprocess.Popen(["xdg-open", target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        log.warning("opening %s failed: %s", target, e)


DESKTOP_ID = "org.multidrop.Multidrop"


def open_ui(url: str) -> None:
    """Show the UI: in the desktop app if installed (tools/install-desktop.sh),
    else in the browser. The app finds the daemon on its own."""
    dirs = [os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share"),
            *(os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")]
    installed = any((Path(d) / "applications" / f"{DESKTOP_ID}.desktop").exists()
                    for d in dirs if d)
    if not WINDOWS and installed and shutil.which("gtk-launch"):
        try:
            subprocess.Popen(["gtk-launch", DESKTOP_ID], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        except OSError as e:
            log.warning("gtk-launch failed: %s", e)
    _open(url)


async def run(cfg: Config, *, notifications: bool | None = None,
              started: asyncio.Future | None = None,
              stop: asyncio.Event | None = None, bus_address: str | None = None) -> None:
    """Run until `stop` is set (or SIGINT/SIGTERM). `started` gets the web URL."""
    rt = runtime_dir()
    rt.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not WINDOWS:     # per-user profile directory there
        if rt.stat().st_uid != os.getuid():
            raise PermissionError(f"{rt} belongs to another user")
        os.chmod(rt, 0o700)
    sock = socket_path(cfg)
    sock.parent.mkdir(parents=True, exist_ok=True)
    _check_not_running(sock)
    token = load_or_create_token(cfg.state_dir / "web-token")
    url_file = sock.parent / "url"

    daemon = Daemon(cfg)
    app = build_app(daemon, token)
    url_holder: list[str] = [""]

    notifier = None
    # Desktop notifications go through D-Bus (Linux); elsewhere the UI asks.
    if (cfg.daemon.notifications if notifications is None else notifications) and not WINDOWS:
        from .notify import Notifier
        notifier = Notifier(daemon.answer, lambda: open_ui(url_holder[0]),
                            lambda: _open(str(cfg.download_dir)), bus_address)
        try:
            await notifier.start()
            daemon.notifier = notifier
        except Exception as e:
            log.warning("desktop notifications unavailable (%s); use the web UI", e)
            await notifier.stop()
            notifier = None

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (RuntimeError, ValueError, NotImplementedError):  # not main thread; Windows
            pass

    runner = web.AppRunner(app, handle_signals=False)
    own_sock: int | None = None     # inode of our socket: never unlink another daemon's
    try:
        await daemon.start()
        await runner.setup()
        tcp = web.TCPSite(runner, "127.0.0.1", cfg.daemon.web_port)
        try:
            await tcp.start()
        except OSError as e:
            if e.errno != errno.EADDRINUSE or not cfg.daemon.web_port:
                raise
            tcp = web.TCPSite(runner, "127.0.0.1", 0)
            await tcp.start()
            log.warning("web UI port %d is in use, using a random port", cfg.daemon.web_port)
        port = next(a[1] for a in runner.addresses if isinstance(a, tuple))
        app[WEB_PORT][0] = port
        if not WINDOWS:     # local API socket; on Windows the token-guarded HTTP API only
            old_umask = os.umask(0o077)
            try:
                await web.UnixSite(runner, str(sock)).start()
                own_sock = sock.stat().st_ino
            finally:
                os.umask(old_umask)
        url_holder[0] = f"http://127.0.0.1:{port}/#t={token}"
        fd = os.open(url_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(url_holder[0] + "\n")
        log.info("web UI: http://127.0.0.1:%d/ (URL with token in %s)", port, url_file)
        if not WINDOWS:
            log.info("API socket: %s", sock)
        if started is not None and not started.done():
            started.set_result(url_holder[0])
        await stop.wait()
    finally:
        await runner.cleanup()
        await daemon.stop()
        if notifier is not None:
            await notifier.stop()
        # Another daemon may have replaced both meanwhile (two started at once
        # both pass _check_not_running); removing its files would leave it
        # running but unreachable.
        try:
            if own_sock is not None and sock.stat().st_ino == own_sock:
                sock.unlink()
                url_file.unlink(missing_ok=True)
            elif WINDOWS and url_holder[0] and url_file.read_text().strip() == url_holder[0]:
                url_file.unlink()
        except FileNotFoundError:
            pass


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="multidropd", description=__doc__.splitlines()[0])
    p.add_argument("--config", help="config file (default ~/.config/multidrop/config.toml)")
    p.add_argument("--web-port", type=int, help="web UI port on 127.0.0.1 (default 53319)")
    p.add_argument("--accept", choices=("ask", "all"), help="incoming transfers")
    p.add_argument("--no-notify", action="store_true", help="no desktop notifications")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = Config.load(Path(args.config) if args.config else None)
    if args.web_port is not None:
        cfg.daemon.web_port = args.web_port
    if args.accept:
        cfg.daemon.accept = args.accept
    try:
        asyncio.run(run(cfg, notifications=False if args.no_notify else None))
    except AlreadyRunning as e:
        log.error("%s", e)
        return 1
    return 0
