"""MultiDrop: multidrop as a desktop app, like AirDrop on a Mac.

An icon in the top bar (StatusNotifierItem, shown by Ubuntu's AppIndicator
extension) with a menu: nearby devices, AirDrop on/off, received files. The
window shows nearby devices as bubbles; drop files on one, or click it, to
send. Files can also be dropped on the dock icon or sent from Nautilus
(Scripts > "Send with MultiDrop").

Runs under the system Python (GTK 4 and libadwaita come from Ubuntu's
python3-gi), not the venv, so this module only uses the standard library plus
gi. It talks to the venv's multidropd over its Unix socket, starts it when
needed, and runs timed AirDrop sessions via `multidrop airdrop run`.

    /usr/bin/python3 src/multidrop/desktop.py --venv ~/multidrop/.venv [FILES...]

Installed by tools/install-desktop.sh.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

APP_ID = "org.multidrop.Multidrop"
APP_NAME = "MultiDrop"
PROTO = {"airdrop": "AirDrop", "quickshare": "Quick Share", "localsend": "LocalSend"}
AIRDROP_MINUTES = (10, 30, 60)


def runtime_dir() -> Path:
    # Same as multidrop.config.runtime_dir (not imported: the venv's
    # dependencies are not available under the system Python).
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/multidrop-{os.getuid()}"
    return Path(base) / "multidrop"


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(base) / "multidrop"


def daemon_pid(sock: Path) -> int | None:
    """PID of the process serving the API socket, None if nobody answers."""
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(sock))
            cred = s.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
    except OSError:
        return None
    pid, uid, _ = struct.unpack("3i", cred)
    return pid if uid == os.getuid() else None


def is_airdrop_session(pid: int) -> bool:
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return b"airdrop" in argv and b"run" in argv


def log_tail(path: Path, lines: int = 8) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1000
    return ""


# --- multidropd API over the Unix socket -------------------------------------

class ApiError(Exception):
    pass


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(self.timeout)
        s.connect(str(self._path))
        self.sock = s


class Api:
    """Blocking client; the UI calls it from worker threads."""

    def __init__(self, sock: Path):
        self.sock = sock

    def request(self, method: str, path: str, body: dict | None = None,
                timeout: float = 30):
        conn = _UnixHTTP(self.sock, timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if data else {}
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            raw = r.read()
        except OSError as e:
            raise ApiError(f"multidropd not reachable ({e})") from e
        finally:
            conn.close()
        try:
            out = json.loads(raw) if raw else None
        except ValueError:
            out = None
        if r.status >= 400:
            msg = out.get("error") if isinstance(out, dict) else None
            raise ApiError(msg or f"HTTP {r.status}")
        return out

    def events(self, since: int = 0) -> Iterator[dict]:
        """Server-sent events until the daemon closes the stream."""
        conn = _UnixHTTP(self.sock, timeout=45)      # the daemon pings every 15 s
        try:
            conn.request("GET", f"/api/events?since={since}")
            r = conn.getresponse()
            if r.status != 200:
                raise ApiError(f"HTTP {r.status}")
            while line := r.readline():
                if line.startswith(b"data: "):
                    try:
                        yield json.loads(line[6:])
                    except ValueError:
                        pass
        finally:
            conn.close()


# --- background processes ----------------------------------------------------

class Services:
    """The plain multidropd and timed AirDrop sessions (`multidrop airdrop run`)."""

    def __init__(self, venv: Path):
        self.venv = venv
        self.sock = runtime_dir() / "multidropd.sock"
        self.logs = state_dir()
        self.daemon: subprocess.Popen | None = None
        self.session: subprocess.Popen | None = None
        self.session_end = 0.0
        self.daemon_started = 0.0
        self.switching = False      # start_airdrop runs: don't restart multidropd

    def spawn(self, argv: list[str], log_name: str) -> subprocess.Popen:
        self.logs.mkdir(parents=True, exist_ok=True)
        with open(self.logs / log_name, "ab") as log:
            # Own session: survives the app, gets no terminal signals.
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)

    def command(self, name: str) -> str:
        """The venv's program in a checkout, else the installed one (.deb: /usr/bin)."""
        local = self.venv / "bin" / name
        if local.exists():
            return str(local)
        import shutil
        return shutil.which(name) or str(local)

    def pid(self) -> int | None:
        return daemon_pid(self.sock)

    def airdrop_on(self) -> bool:
        if self.session is not None and self.session.poll() is None:
            return True
        pid = self.pid()
        return pid is not None and is_airdrop_session(pid)

    def minutes_left(self) -> int | None:
        if self.session is None:
            return None
        return max(0, round((self.session_end - time.time()) / 60))

    def orphans(self) -> list[int]:
        """Our multidropd processes that do not own the socket (two started
        at once both pass the daemon's socket check; the loser lingers and
        advertises this PC as a peer)."""
        owner, out = self.pid(), []
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit() or int(proc.name) == owner:
                continue
            try:
                if proc.stat().st_uid != os.getuid():
                    continue
                argv = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            # Only default instances (a --config one has its own socket).
            if any(a.endswith(b"bin/multidropd") for a in argv[:2]) and b"--config" not in argv:
                out.append(int(proc.name))
        return out

    def ensure_daemon(self) -> None:
        if self.switching or self.session is not None:
            return
        if self.pid() is not None:
            for pid in self.orphans():
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            return
        if self.daemon is not None and self.daemon.poll() is None \
                and time.time() - self.daemon_started > 20:
            # Running but without a socket (lost it to another instance): restart.
            self.daemon.terminate()
            try:
                self.daemon.wait(5)
            except subprocess.TimeoutExpired:
                self.daemon.kill()
        if self.daemon is None or self.daemon.poll() is not None:
            self.daemon = self.spawn([self.command("multidropd")], "desktop-daemon.log")
            self.daemon_started = time.time()

    def start_airdrop(self, minutes: int) -> None:
        self.switching = True
        try:
            self._start_airdrop(minutes)
        finally:
            self.switching = False

    def _start_airdrop(self, minutes: int) -> None:
        pid = self.pid()
        if pid is not None:
            if is_airdrop_session(pid):
                return
            os.kill(pid, signal.SIGTERM)        # clean exit: systemd won't restart it
            deadline = time.time() + 8
            while self.pid() is not None and time.time() < deadline:
                time.sleep(0.1)
            if self.pid() is not None:
                raise ApiError("multidropd could not be stopped")
        if self.daemon is not None:
            self.daemon.poll()
            self.daemon = None
        self.session = self.spawn([self.command("multidrop"), "airdrop", "run",
                                   "--minutes", str(minutes), "--switch-channel"], "desktop-airdrop.log")
        self.session_end = time.time() + minutes * 60

    def stop_airdrop(self) -> None:
        """SIGTERM: the daemon stops and the radio is restored (see airdrop_radio)."""
        if self.session is not None and self.session.poll() is None:
            self.session.send_signal(signal.SIGTERM)
            return
        pid = self.pid()
        if pid is not None and is_airdrop_session(pid):
            os.kill(pid, signal.SIGTERM)

    def poll_session(self) -> int | None:
        """Exit code once, when our AirDrop session has ended."""
        if self.session is None or self.session.poll() is None:
            return None
        code = self.session.returncode
        self.session = None
        return code


# Pseudo device: Quick Share by QR code (the phone scans, then advertises
# itself with a token from the code; see backends/quickshare/qr.py).
DEVICE_TTL = 90      # seconds a device stays listed after it was last found
QR_DEVICE = {"name": "Samsung/Android via QR code", "qr": True, "targets": [{"protocol": "quickshare"}]}


# --- AirPlay screen mirroring (doubletake) -----------------------------------



def doubletake_bin(name: str) -> str | None:
    """tools/fetch-doubletake.sh installs to ~/.local/lib, packages to $PATH."""
    local = Path.home() / ".local/lib/multidrop/doubletake" / name
    if os.access(local, os.X_OK):
        return str(local)
    import shutil
    return shutil.which(name)


def is_mirror_target(device: dict, caps: dict[str, dict]) -> bool:
    """caps: multidropd's /api/airplay entries by device id and IP. Receivers
    that announce no screen mirroring (speakers) are hidden; unknown ones are
    shown, so nothing disappears just because its mDNS answer was late."""
    info = caps.get((device.get("device_id") or "").upper()) or caps.get(device.get("ip") or "")
    return info is None or info.get("screen") is not False


class Mirror:
    """doubletake's daemon: discovery, pairing PIN, streams. JSON request and
    response over $XDG_RUNTIME_DIR/doubletake.sock (doubletake-ctl's protocol)."""

    def __init__(self, sock: Path | None = None):
        rt = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        self.sock = sock or Path(rt) / "doubletake.sock"
        self.proc: subprocess.Popen | None = None

    def request(self, cmd: str, timeout: float = 30, **fields) -> dict:
        req = {"cmd": cmd, **{k: v for k, v in fields.items() if v}}
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(timeout)
                s.connect(str(self.sock))
                s.sendall(json.dumps(req).encode() + b"\n")
                buf = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    try:
                        return json.loads(buf)
                    except ValueError:
                        continue        # incomplete
        except OSError as e:
            raise ApiError(f"doubletake not reachable ({e})") from e
        raise ApiError("doubletake: empty answer")

    def alive(self) -> bool:
        try:
            return bool(self.request("status", timeout=3).get("ok"))
        except ApiError:
            return False

    def ensure(self, log_dir: Path) -> None:
        if self.alive():
            return
        binary = doubletake_bin("doubletake")
        if binary is None:
            raise ApiError("doubletake is missing (run multidrop-fetch-doubletake "
                           "or tools/fetch-doubletake.sh)")
        import shutil
        argv = [binary, "-daemonize", "-socket", str(self.sock)]
        if not shutil.which("pactl"):
            argv.append("-no-audio")        # its audio capture needs pactl
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "doubletake.log", "ab") as log:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        for _ in range(50):
            if self.alive():
                return
            if self.proc.poll() is not None:
                raise ApiError(f"doubletake exited: {log_tail(log_dir / 'doubletake.log', 2)}")
            time.sleep(0.1)
        raise ApiError("doubletake does not start")

    def stop(self) -> None:
        """Ends streams; stops the daemon only if we started it."""
        try:
            self.request("disconnect", timeout=5)
        except ApiError:
            pass
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --- AirPlay audio (PipeWire RAOP) ------------------------------------------

RAOP_CONF = """# multidrop: AirPlay speakers as PipeWire sinks (separate client process,
# like filter-chain; the session's own PipeWire is not restarted).
context.properties = { log.level = 2 }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-raop-discover }
]
"""
RAOP_PREFIX = "raop_sink."


def parse_sinks(dump: list) -> tuple[list[dict], str, str]:
    """pw-dump -> (AirPlay sinks, default sink name, configured default JSON)."""
    sinks, default, configured = [], "", ""
    for o in dump:
        props = (o.get("info") or {}).get("props") or {}
        if o.get("type") == "PipeWire:Interface:Node" and props.get("media.class") == "Audio/Sink" \
                and str(props.get("node.name", "")).startswith(RAOP_PREFIX):
            sinks.append({"id": o["id"], "node": props["node.name"],
                          "name": props.get("node.description") or props["node.name"]})
        if o.get("type") == "PipeWire:Interface:Metadata" \
                and (o.get("props") or {}).get("metadata.name") == "default":
            for m in o.get("metadata") or []:
                if m.get("key") == "default.audio.sink":
                    default = (m.get("value") or {}).get("name", "")
                elif m.get("key") == "default.configured.audio.sink":
                    configured = json.dumps(m.get("value"))
    sinks.sort(key=lambda d: d["name"].lower())
    return sinks, default, configured


class Speakers:
    """AirPlay speakers via PipeWire's raop-discover in our own pipewire
    process; choosing one makes it the default output (wpctl), "Dieser PC"
    restores the previously configured default (pw-metadata)."""

    def __init__(self):
        import shutil
        self.ok = all(shutil.which(t) for t in ("pipewire", "pw-dump", "wpctl", "pw-metadata"))
        self.proc: subprocess.Popen | None = None
        self.previous: str | None = None    # configured default before we switched
        self._configured = ""
        self.conf = runtime_dir() / "airplay-audio.conf"

    def running(self) -> bool:
        """Ours, or one left by an earlier app instance (no duplicate sinks)."""
        if self.proc is not None and self.proc.poll() is None:
            return True
        want = str(self.conf).encode()
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                if proc.stat().st_uid == os.getuid() and \
                        want in (proc / "cmdline").read_bytes().split(b"\0"):
                    return True
            except OSError:
                continue
        return False

    def ensure(self, log_dir: Path) -> None:
        if self.running():
            return
        conf = self.conf
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(RAOP_CONF)
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "airplay-audio.log", "ab") as log:
            self.proc = subprocess.Popen(["pipewire", "-c", str(conf)], stdin=subprocess.DEVNULL,
                                         stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)

    def state(self) -> tuple[list[dict], str]:
        out = subprocess.run(["pw-dump"], capture_output=True, timeout=10).stdout
        sinks, default, configured = parse_sinks(json.loads(out or b"[]"))
        self._configured = configured
        return sinks, default

    def play_on(self, sink: dict) -> None:
        if self.previous is None:
            self.state()
            self.previous = self._configured or ""
        subprocess.run(["wpctl", "set-default", str(sink["id"])], check=True, timeout=10)

    def back_to_pc(self) -> None:
        if self.previous:
            subprocess.run(["pw-metadata", "-n", "default", "0", "default.configured.audio.sink",
                            self.previous, "Spa:String:JSON"], capture_output=True, timeout=10)
        else:
            subprocess.run(["pw-metadata", "-n", "default", "-d", "0",
                            "default.configured.audio.sink"], capture_output=True, timeout=10)
        self.previous = None

    def volume(self, sink_id: int, value: float | None = None) -> float:
        if value is not None:
            subprocess.run(["wpctl", "set-volume", str(sink_id), f"{value:.2f}"], timeout=10)
            return value
        out = subprocess.run(["wpctl", "get-volume", str(sink_id)], capture_output=True,
                             text=True, timeout=10).stdout     # "Volume: 0.40"
        try:
            return float(out.split()[1])
        except (IndexError, ValueError):
            return 1.0

    def stop(self) -> None:
        if self.previous is not None:
            self.back_to_pc()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# --- GTK app -----------------------------------------------------------------

CSS = """
.radar-ring {
  border-radius: 999px;
  padding: 14px;
  background: alpha(@accent_bg_color, 0.10);
  animation: radar 2.6s ease-out infinite;
}
@keyframes radar {
  from { box-shadow: 0 0 0 0 alpha(@accent_bg_color, 0.35); }
  to   { box-shadow: 0 0 0 34px alpha(@accent_bg_color, 0); }
}
.device {
  padding: 14px 10px 10px 10px;
  border-radius: 20px;
  transition: background 150ms;
}
.device:hover { background: alpha(currentColor, 0.06); }
.device:drop(active) {
  background: alpha(@accent_bg_color, 0.16);
  box-shadow: inset 0 0 0 2px @accent_bg_color;
}
.device-name { font-weight: 600; }
.pill {
  padding: 3px 11px;
  border-radius: 999px;
  font-size: 0.85em;
  background: alpha(currentColor, 0.07);
  color: alpha(currentColor, 0.55);
}
.pill.on { background: alpha(@success_color, 0.16); color: @success_color; }
.qr { background: white; padding: 12px; border-radius: 12px; }
.pending-bar { background: alpha(@accent_bg_color, 0.12); padding: 8px 14px; }
flowboxchild { border-radius: 20px; }
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="multidrop-desktop", description=__doc__.splitlines()[0])
    p.add_argument("--venv", default=str(Path(__file__).resolve().parents[2] / ".venv"),
                   help="venv with multidrop installed (default: the checkout's .venv)")
    p.add_argument("--background", action="store_true",
                   help="only the top bar icon, no window (autostart)")
    p.add_argument("files", nargs="*", help="files to send (opens the device choice)")
    args = p.parse_args(argv)

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk

    services = Services(Path(args.venv).expanduser())
    api = Api(services.sock)
    mirror = Mirror()
    speakers = Speakers()

    def in_thread(fn, done=None) -> None:
        """Run fn() in a thread; done(result, error) back on the GTK thread."""
        def work():
            try:
                result, error = fn(), None
            except Exception as e:      # shown to the user, never fatal
                result, error = None, e
            if done is not None:
                GLib.idle_add(lambda: done(result, error) and False)
        threading.Thread(target=work, daemon=True).start()

    # --- compatibility: GTK 4.6+ / libadwaita 1.1+ (Ubuntu 22.04, Debian 12,
    # Fedora, Arch, openSUSE); newer widgets only where available.
    ADW = (Adw.get_major_version(), Adw.get_minor_version())
    GTK = (Gtk.get_major_version(), Gtk.get_minor_version())
    if os.environ.get("MULTIDROP_OLD_GTK"):     # development aid: exercise the fallbacks
        ADW, GTK = (1, 1), (4, 6)

    def launch_uri(uri: str) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as e:
            print(f"cannot open {uri}: {e.message}", file=sys.stderr)

    def open_path(path: str, containing: bool = False) -> None:
        if containing:
            # FileManager1: Nautilus, Dolphin, Nemo, Caja, Thunar select the file.
            try:
                Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
                    "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1", "ShowItems",
                    GLib.Variant("(ass)", ([Gio.File.new_for_path(path).get_uri()], "")),
                    None, Gio.DBusCallFlags.NONE, 3000, None)
                return
            except GLib.Error:
                path = str(Path(path).parent)
        launch_uri(Gio.File.new_for_path(path).get_uri())

    class Dialog:
        """Adw.AlertDialog (1.5+) or Adw.MessageDialog."""

        def __init__(self, parent, heading: str, body: str, responses: list[tuple[str, str]],
                     suggested: str | None = None, on_response=None, extra=None):
            if ADW >= (1, 5):
                self.dlg = Adw.AlertDialog(heading=heading, body=body)
            else:
                self.dlg = Adw.MessageDialog(transient_for=parent, heading=heading, body=body,
                                             modal=True)
            self.parent = parent
            for rid, label in responses:
                self.dlg.add_response(rid, label)
            if suggested:
                self.dlg.set_response_appearance(suggested, Adw.ResponseAppearance.SUGGESTED)
                self.dlg.set_default_response(suggested)
            self.dlg.set_close_response(responses[0][0])
            if extra is not None and ADW >= (1, 2):
                self.dlg.set_extra_child(extra)
            if on_response:
                self.dlg.connect("response", lambda _d, r: on_response(r))

        def present(self) -> None:
            if ADW >= (1, 5):
                self.dlg.present(self.parent)
            else:
                self.dlg.present()

        def close(self) -> None:
            if ADW >= (1, 5):
                self.dlg.force_close()
            else:
                self.dlg.close()

    def pick_files(parent, title: str, on_paths) -> None:
        if GTK >= (4, 10):
            dialog = Gtk.FileDialog(title=title)

            def chosen(dlg, result):
                try:
                    files = dlg.open_multiple_finish(result)
                except GLib.Error:
                    return              # cancelled
                paths = [f.get_path() for f in files if f.get_path()]
                if paths:
                    on_paths(paths)
            dialog.open_multiple(parent, None, chosen)
            return
        chooser = Gtk.FileChooserNative(title=title, transient_for=parent,
                                        action=Gtk.FileChooserAction.OPEN, select_multiple=True)

        def response(dlg, code):
            if code == Gtk.ResponseType.ACCEPT:
                files = dlg.get_files()
                paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
                if any(paths):
                    on_paths([p for p in paths if p])
            dlg.destroy()
            pick_files.keep.discard(dlg)
        chooser.connect("response", response)
        pick_files.keep.add(chooser)    # the native chooser needs a live reference
        chooser.show()
    pick_files.keep = set()

    def load_css(provider) -> None:
        if hasattr(provider, "load_from_string"):
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode(), -1)

    def clear_box(box) -> None:
        if hasattr(box, "remove_all"):
            box.remove_all()
            return
        while (child := box.get_first_child()) is not None:
            box.remove(child)

    class Banner(Gtk.Revealer):
        """Adw.Banner look-alike for libadwaita < 1.3."""

        def __init__(self, on_click):
            super().__init__()
            bar = Gtk.Box(spacing=12)
            bar.add_css_class("pending-bar")
            self.label = Gtk.Label(hexpand=True, xalign=0, wrap=True)
            self.button = Gtk.Button()
            self.button.connect("clicked", lambda *_: on_click(self))
            bar.append(self.label)
            bar.append(self.button)
            self.set_child(bar)

        def set_title(self, text: str) -> None:
            self.label.set_label(text)

        def set_button_label(self, text: str) -> None:
            self.button.set_label(text)

        def set_revealed(self, on: bool) -> None:
            self.set_reveal_child(on)

    def tray_host() -> bool:
        """A StatusNotifier host runs (KDE, XFCE, Cinnamon, MATE, Budgie,
        GNOME with the AppIndicator extension)."""
        try:
            r = Gio.bus_get_sync(Gio.BusType.SESSION).call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                None, Gio.DBusCallFlags.NONE, 2000, None)
            return bool(r.unpack()[0])
        except GLib.Error:
            return False

    # --- top bar icon: StatusNotifierItem + com.canonical.dbusmenu -----------

    SNI_XML = """<node>
      <interface name="org.kde.StatusNotifierItem">
        <property name="Category" type="s" access="read"/>
        <property name="Id" type="s" access="read"/>
        <property name="Title" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <property name="IconName" type="s" access="read"/>
        <property name="IconThemePath" type="s" access="read"/>
        <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
        <property name="ItemIsMenu" type="b" access="read"/>
        <property name="Menu" type="o" access="read"/>
        <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
        <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
        <signal name="NewIcon"/><signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal>
      </interface>
      <interface name="com.canonical.dbusmenu">
        <property name="Version" type="u" access="read"/>
        <property name="TextDirection" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <property name="IconThemePath" type="as" access="read"/>
        <method name="GetLayout">
          <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
          <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
        </method>
        <method name="GetGroupProperties">
          <arg type="ai" direction="in"/><arg type="as" direction="in"/>
          <arg type="a(ia{sv})" direction="out"/>
        </method>
        <method name="GetProperty">
          <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
        </method>
        <method name="Event">
          <arg type="i" direction="in"/><arg type="s" direction="in"/>
          <arg type="v" direction="in"/><arg type="u" direction="in"/>
        </method>
        <method name="EventGroup">
          <arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/>
        </method>
        <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
        <method name="AboutToShowGroup">
          <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
        </method>
        <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
        <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
      </interface>
    </node>"""

    class Tray:
        PATH = "/StatusNotifierItem"
        MENU = "/MenuBar"

        def __init__(self, app: MultidropApp):
            self.app = app
            self.items: dict[int, dict] = {}
            self.actions: dict[int, callable] = {}
            self.layout: list = []
            self.revision = 1
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            info = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
            self.bus.register_object(self.PATH, info.lookup_interface("org.kde.StatusNotifierItem"),
                                     self.on_call, self.on_get, None)
            self.bus.register_object(self.MENU, info.lookup_interface("com.canonical.dbusmenu"),
                                     self.on_call, self.on_get, None)
            self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
            Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE,
                                           None, None)
            # The extension may start after us (or restart): register again.
            Gio.bus_watch_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher",
                                             Gio.BusNameWatcherFlags.NONE,
                                             lambda *_: self.register(), None)
            self.rebuild()

        def register(self) -> None:
            self.bus.call("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                          "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                          GLib.Variant("(s)", (self.name,)), None,
                          Gio.DBusCallFlags.NONE, -1, None, None)

        # menu model: list of (props, action, children)
        def rebuild(self) -> None:
            self.items.clear()
            self.actions.clear()
            next_id = iter(range(1, 10_000))

            def item(label="", action=None, children=None, **props):
                i = next(next_id)
                p = dict(props)
                if label:
                    p["label"] = label
                self.items[i] = p
                if action:
                    self.actions[i] = action
                return (i, [c for c in children or []])

            def sep():
                return item(type="separator")

            st = self.app.state
            menu = []
            menu.append(item(st.headline(), enabled=False))
            menu.append(sep())
            if st.devices:
                for d in st.devices[:12]:
                    protos = " · ".join(PROTO.get(t["protocol"], t["protocol"])
                                        for t in d["targets"])
                    menu.append(item(f"{d['name']}  —  {protos}",
                                     lambda d=d: self.app.pick_and_send(d)))
            else:
                menu.append(item("Looking for devices …" if st.searching
                                 else "No devices nearby", enabled=False))
            menu.append(item("Search again", self.app.refresh_devices))
            menu.append(sep())
            if services.airdrop_on():
                left = services.minutes_left()
                label = "AirDrop on" + (f" ({left} min left)" if left is not None else "")
                menu.append(item(label, self.app.airdrop_off,
                                 **{"toggle-type": "checkmark", "toggle-state": 1}))
            else:
                subs = [item(f"for {m} minutes", lambda m=m: self.app.airdrop_on(m))
                        for m in AIRDROP_MINUTES]
                menu.append(item("Turn AirDrop on", None, subs,
                                 **{"children-display": "submenu"}))
            menu.append(sep())
            for stream in st.streaming():
                menu.append(item(f"Stop mirroring to {stream.get('device') or stream.get('device_ip')}", lambda ip=stream.get("device_ip"): self.app.mirror_stop(ip)))
            if st.mirror_ok:
                busy = {s.get("device_ip") for s in st.streams}
                subs = [item(f"{d['name']}" + (f"  —  {d['model']}" if d.get("model") else ""),
                             lambda d=d: self.app.mirror_start(d))
                        for d in st.screens if d["ip"] not in busy]
                if not subs:
                    subs = [item("No AirPlay screens found", enabled=False)]
                menu.append(item("Mirror screen", None, subs,
                                 **{"children-display": "submenu"}))
            if st.audio_ok:
                current = st.playing_on()
                radio = {"toggle-type": "radio"}
                subs = [item("This computer", self.app.audio_pc,
                             **radio, **{"toggle-state": int(current is None)})]
                subs += [item(d["name"], lambda d=d: self.app.audio_on(d),
                              **radio, **{"toggle-state": int(current is d)})
                         for d in st.speakers]
                label = "Play audio on" + (f": {current['name']}" if current else "")
                menu.append(item(label, None, subs, **{"children-display": "submenu"}))
            menu.append(sep())
            menu.append(item(f"Open {APP_NAME}", self.app.show_window))
            menu.append(item("Received files", lambda: open_path(st.download_dir)))
            menu.append(sep())
            menu.append(item("Quit", self.app.quit_all))
            self.layout = menu
            self.revision += 1
            self.bus.emit_signal(None, self.MENU, "com.canonical.dbusmenu", "LayoutUpdated",
                                 GLib.Variant("(ui)", (self.revision, 0)))

        def props(self, i: int) -> dict:
            p = {"visible": True, "enabled": True, **self.items.get(i, {})}
            out = {}
            for k, v in p.items():
                if isinstance(v, bool):
                    out[k] = GLib.Variant("b", v)
                elif isinstance(v, int):
                    out[k] = GLib.Variant("i", v)
                else:
                    out[k] = GLib.Variant("s", v)
            return out

        def node(self, i: int, children: list, depth: int) -> tuple:
            kids = [GLib.Variant("(ia{sv}av)", self.node(c, cc, depth - 1))
                    for c, cc in children] if depth != 0 else []
            props = self.props(i) if i else {"children-display": GLib.Variant("s", "submenu")}
            return (i, props, kids)

        def find(self, i: int, nodes: list) -> list | None:
            for n, kids in nodes:
                if n == i:
                    return kids
                if (found := self.find(i, kids)) is not None:
                    return found
            return None

        def on_call(self, _bus, _sender, _path, iface, method, params, inv) -> None:
            a = params.unpack()
            if method in ("Activate", "SecondaryActivate"):
                self.app.show_window()
                inv.return_value(None)
            elif method in ("ContextMenu", "Scroll"):
                inv.return_value(None)
            elif method == "GetLayout":
                parent, depth = a[0], a[1]
                kids = self.layout if parent == 0 else (self.find(parent, self.layout) or [])
                inv.return_value(GLib.Variant("(u(ia{sv}av))", (
                    self.revision, self.node(parent, kids, depth))))
            elif method == "GetGroupProperties":
                ids = a[0] or list(self.items)
                inv.return_value(GLib.Variant("(a(ia{sv}))", (
                    [(i, self.props(i)) for i in ids if i in self.items],)))
            elif method == "GetProperty":
                v = self.props(a[0]).get(a[1])
                inv.return_value(GLib.Variant("(v)", (v or GLib.Variant("s", ""),)))
            elif method == "Event":
                if a[1] == "clicked":
                    self.click(a[0])
                inv.return_value(None)
            elif method == "EventGroup":
                for i, ev, _d, _t in a[0]:
                    if ev == "clicked":
                        self.click(i)
                inv.return_value(GLib.Variant("(ai)", ([],)))
            elif method == "AboutToShow":
                if a[0] == 0:
                    self.app.refresh_devices()
                inv.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                inv.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

        def click(self, i: int) -> None:
            action = self.actions.get(i)
            if action:
                GLib.idle_add(lambda: action() and False)

        def on_get(self, _bus, _sender, _path, iface, prop):
            icons = str(Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
                        / "icons")
            if iface == "com.canonical.dbusmenu":
                return {"Version": GLib.Variant("u", 3),
                        "TextDirection": GLib.Variant("s", "ltr"),
                        "Status": GLib.Variant("s", "normal"),
                        "IconThemePath": GLib.Variant("as", [icons])}.get(prop)
            return {"Category": GLib.Variant("s", "ApplicationStatus"),
                    "Id": GLib.Variant("s", APP_NAME.lower()),
                    "Title": GLib.Variant("s", APP_NAME),
                    "Status": GLib.Variant("s", "Active"),
                    "IconName": GLib.Variant("s", APP_ID + "-symbolic"),
                    "IconThemePath": GLib.Variant("s", icons),
                    "ToolTip": GLib.Variant("(sa(iiay)ss)", (
                        "", [], APP_NAME, self.app.state.headline())),
                    "ItemIsMenu": GLib.Variant("b", False),
                    "Menu": GLib.Variant("o", self.MENU)}.get(prop)

    # --- shared state -----------------------------------------------------

    class State:
        def __init__(self):
            self.alias = ""
            self.protocols: set[str] = set()
            self.download_dir = str(Path.home() / "Downloads")
            self.devices: list[dict] = []
            self.searching = False
            self.connected = False
            self.mirror_ok = doubletake_bin("doubletake") is not None
            self.mirror_error = ""
            self.screens: list[dict] = []       # AirPlay receivers with a screen
            self.streams: list[dict] = []       # doubletake's active streams
            self.audio_ok = speakers.ok
            self.speakers: list[dict] = []      # AirPlay sinks
            self.audio_default = ""             # node.name of the default output

        def playing_on(self) -> dict | None:
            return next((d for d in self.speakers if d["node"] == self.audio_default), None)

        def streaming(self) -> list[dict]:
            return [s for s in self.streams if s.get("state") in ("connecting", "streaming")]

        def headline(self) -> str:
            if not self.connected:
                return f"{APP_NAME} is starting …"
            return f"Visible as “{self.alias}”"

    # --- window -----------------------------------------------------------

    class DeviceBubble(Gtk.Box):
        def __init__(self, app: MultidropApp, device: dict):
            super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                             halign=Gtk.Align.CENTER, width_request=132)
            self.app = app
            self.device = device
            self.add_css_class("device")
            avatar = Adw.Avatar(size=76, text=device["name"], show_initials=not device.get("qr"))
            if device.get("qr"):
                avatar.set_icon_name("camera-photo-symbolic")
            self.append(avatar)
            name = Gtk.Label(label=device["name"], wrap=True, lines=2, justify=Gtk.Justification.CENTER,
                             ellipsize=3, max_width_chars=16)
            name.add_css_class("device-name")
            self.append(name)
            kinds = "Scan the QR code with the camera" if device.get("qr") else \
                " · ".join(PROTO.get(t["protocol"], t["protocol"]) for t in device["targets"])
            sub = Gtk.Label(label=kinds, wrap=True, justify=Gtk.Justification.CENTER,
                            max_width_chars=18)
            sub.add_css_class("caption")
            sub.add_css_class("dim-label")
            self.append(sub)
            self.progress = Gtk.ProgressBar(visible=False, margin_top=4)
            self.append(self.progress)
            self.set_tooltip_text("Drop files here or click")

            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
            drop.connect("drop", self.on_drop)
            self.add_controller(drop)
            click = Gtk.GestureClick()
            click.connect("released", lambda *_: app.pick_and_send(device))
            self.add_controller(click)

        def on_drop(self, _target, value, _x, _y) -> bool:
            paths = [f.get_path() for f in value.get_files() if f.get_path()]
            if paths:
                self.app.send(self.device, paths)
            return bool(paths)

    class Window(Adw.ApplicationWindow):
        def __init__(self, app: MultidropApp):
            super().__init__(application=app, title=APP_NAME,
                             default_width=820, default_height=640)
            self.app = app
            self.bubbles: dict[str, DeviceBubble] = {}
            self.pending: list[str] = []

            if ADW >= (1, 4):
                view = Adw.ToolbarView()
            else:
                view = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                view.add_top_bar = view.append
                view.set_content = lambda w: (w.set_vexpand(True), view.append(w))
            header = Adw.HeaderBar()
            self.title = Adw.WindowTitle(title="Nearby", subtitle="")
            header.set_title_widget(self.title)
            refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Search again")
            refresh.connect("clicked", lambda *_: app.refresh_devices())
            header.pack_end(refresh)
            folder = Gtk.Button(icon_name="folder-download-symbolic",
                                tooltip_text="Received files")
            folder.connect("clicked", lambda *_: open_path(app.state.download_dir))
            header.pack_end(folder)
            self.mirror_btn = Gtk.MenuButton(tooltip_text="Mirror screen (AirPlay)")
            self.mirror_btn.set_child(Adw.ButtonContent(icon_name="video-display-symbolic",
                                                        label="Mirror"))
            self.mirror_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                                       margin_top=6, margin_bottom=6, margin_start=6,
                                       margin_end=6, width_request=260)
            pop = Gtk.Popover(child=self.mirror_list)
            pop.connect("show", lambda *_: self.fill_mirror_list())
            self.mirror_btn.set_popover(pop)
            header.pack_start(self.mirror_btn)
            self.audio_btn = Gtk.MenuButton(tooltip_text="Play audio via AirPlay")
            self.audio_btn.set_child(Adw.ButtonContent(icon_name="audio-speakers-symbolic",
                                                       label="Audio"))
            self.audio_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                                      margin_top=6, margin_bottom=6, margin_start=6,
                                      margin_end=6, width_request=260)
            pop = Gtk.Popover(child=self.audio_list)
            pop.connect("show", lambda *_: self.fill_audio_list())
            self.audio_btn.set_popover(pop)
            header.pack_start(self.audio_btn)
            view.add_top_bar(header)

            if ADW >= (1, 3):
                self.banner = Adw.Banner()
                self.banner.connect("button-clicked", self.on_banner)
            else:
                self.banner = Banner(self.on_banner)
            view.add_top_bar(self.banner)

            self.pending_bar = Gtk.Revealer()
            bar = Gtk.Box(spacing=12)
            bar.add_css_class("pending-bar")
            self.pending_label = Gtk.Label(hexpand=True, xalign=0)
            bar.append(self.pending_label)
            cancel = Gtk.Button(label="Cancel")
            cancel.add_css_class("flat")
            cancel.connect("clicked", lambda *_: self.set_pending([]))
            bar.append(cancel)
            self.pending_bar.set_child(bar)
            view.add_top_bar(self.pending_bar)

            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                    max_children_per_line=6, min_children_per_line=2,
                                    column_spacing=8, row_spacing=8, valign=Gtk.Align.START,
                                    halign=Gtk.Align.CENTER, margin_top=28,
                                    margin_start=24, margin_end=24)
            self.empty = Gtk.Label(label="Looking for devices …", margin_top=24,
                                   justify=Gtk.Justification.CENTER, wrap=True)
            self.empty.add_css_class("dim-label")
            devices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)
            devices.append(self.flow)
            devices.append(self.empty)
            scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroll.set_child(devices)
            body.append(scroll)

            radar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                            halign=Gtk.Align.CENTER, margin_bottom=26, margin_top=8)
            ring = Gtk.Box(halign=Gtk.Align.CENTER)
            ring.add_css_class("radar-ring")
            ring.append(Gtk.Image(icon_name=APP_ID, pixel_size=52))
            radar.append(ring)
            hint = Gtk.Label(label="Drop files on a device to send them.",
                             margin_top=10)
            hint.add_css_class("dim-label")
            radar.append(hint)
            self.visible_label = Gtk.Label()
            self.visible_label.add_css_class("heading")
            radar.append(self.visible_label)
            pills = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            self.pills = {}
            for key, label in PROTO.items():
                pill = Gtk.Label(label=label)
                pill.add_css_class("pill")
                pills.append(pill)
                self.pills[key] = pill
            radar.append(pills)
            body.append(radar)

            self.toasts = Adw.ToastOverlay()
            self.toasts.set_child(body)
            view.set_content(self.toasts)
            self.set_content(view)
            # Like a Mac app: closing the window keeps the top bar icon. Without
            # a tray host (plain GNOME) closing ends the app (see on_close).
            self.set_hide_on_close(True)
            self.connect("close-request", self.on_close)
            self.update()

        def on_close(self, _win) -> bool:
            if not tray_host():
                GLib.idle_add(lambda: self.app.quit_all() and False)
            return False

        def fill_audio_list(self) -> None:
            st = self.app.state
            clear_box(self.audio_list)
            title = Gtk.Label(label="Play audio on", xalign=0, margin_start=6,
                              margin_bottom=4)
            title.add_css_class("heading")
            self.audio_list.append(title)
            if not st.audio_ok:
                note = Gtk.Label(label="PipeWire tools are missing\n(pipewire, wpctl, pw-dump)",
                                 xalign=0, margin_start=6)
                note.add_css_class("dim-label")
                self.audio_list.append(note)
                return
            current = st.playing_on()

            def row(label: str, active: bool, action, icon: str) -> None:
                btn = Gtk.Button()
                btn.add_css_class("flat")
                box = Gtk.Box(spacing=10)
                box.append(Gtk.Image(icon_name=icon))
                box.append(Gtk.Label(label=label, xalign=0, hexpand=True))
                check = Gtk.Image(icon_name="object-select-symbolic")
                check.set_opacity(1 if active else 0)
                box.append(check)
                btn.set_child(box)
                btn.connect("clicked", lambda *_: (self.audio_btn.popdown(), action()))
                self.audio_list.append(btn)

            row("This computer", current is None, self.app.audio_pc, "computer-symbolic")
            for d in st.speakers:
                row(d["name"], current is d, lambda d=d: self.app.audio_on(d),
                    "audio-speakers-symbolic")
            if not st.speakers:
                note = Gtk.Label(label="Looking for AirPlay speakers …", xalign=0,
                                 margin_start=6)
                note.add_css_class("dim-label")
                self.audio_list.append(note)
            if current is not None:
                scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.02)
                scale.set_draw_value(False)
                scale.set_margin_top(6)
                sink_id = current["id"]
                in_thread(lambda: speakers.volume(sink_id),
                          lambda v, e: scale.set_value(v if e is None else 1.0))
                scale.connect("value-changed", lambda sc: self.app.audio_volume(
                    sink_id, sc.get_value()))
                box = Gtk.Box(spacing=8, margin_start=6, margin_end=6)
                box.append(Gtk.Image(icon_name="audio-volume-low-symbolic"))
                scale.set_hexpand(True)
                box.append(scale)
                box.append(Gtk.Image(icon_name="audio-volume-high-symbolic"))
                self.audio_list.append(box)

        def fill_mirror_list(self) -> None:
            st = self.app.state
            clear_box(self.mirror_list)
            title = Gtk.Label(label="Mirror screen", xalign=0, margin_start=6,
                              margin_bottom=4)
            title.add_css_class("heading")
            self.mirror_list.append(title)

            def row(label: str, sub: str, action, icon: str) -> None:
                btn = Gtk.Button()
                btn.add_css_class("flat")
                box = Gtk.Box(spacing=10)
                box.append(Gtk.Image(icon_name=icon))
                texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                texts.append(Gtk.Label(label=label, xalign=0))
                if sub:
                    small = Gtk.Label(label=sub, xalign=0)
                    small.add_css_class("caption")
                    small.add_css_class("dim-label")
                    texts.append(small)
                box.append(texts)
                btn.set_child(box)
                btn.connect("clicked", lambda *_: (self.mirror_btn.popdown(), action()))
                self.mirror_list.append(btn)

            if not st.mirror_ok:
                note = Gtk.Label(label="doubletake is missing.\nInstall it with\n"
                                       "multidrop-fetch-doubletake", xalign=0, margin_start=6)
                note.add_css_class("dim-label")
                self.mirror_list.append(note)
                return
            for s in st.streaming():
                ip = s.get("device_ip")
                row(f"Stop {s.get('device') or ip}",
                    "mirroring" if s.get("state") == "streaming" else "connecting …",
                    lambda ip=ip: self.app.mirror_stop(ip), "media-playback-stop-symbolic")
            busy = {s.get("device_ip") for s in st.streams}
            for d in st.screens:
                if d["ip"] not in busy:
                    row(d["name"], d.get("model") or "", lambda d=d: self.app.mirror_start(d),
                        "video-display-symbolic")
            if not st.screens:
                note = Gtk.Label(label=st.mirror_error or "Looking for Apple TVs and "
                                 "AirPlay TVs …", xalign=0, wrap=True, margin_start=6)
                note.add_css_class("dim-label")
                self.mirror_list.append(note)

        def on_banner(self, _banner) -> None:
            if services.airdrop_on():
                self.app.airdrop_off()
            else:
                self.app.airdrop_on(30)

        def set_pending(self, paths: list[str]) -> None:
            self.pending = paths
            if paths:
                what = Path(paths[0]).name if len(paths) == 1 else f"{len(paths)} files"
                self.pending_label.set_markup(
                    f"Send <b>{GLib.markup_escape_text(what)}</b> – click a device")
            self.pending_bar.set_reveal_child(bool(paths))

        def toast(self, text: str, button: str | None = None, action=None) -> None:
            t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=5)
            if button and action:
                t.set_button_label(button)
                t.connect("button-clicked", lambda *_: action())
            self.toasts.add_toast(t)

        def update(self) -> None:
            st = self.app.state
            self.title.set_subtitle(st.headline())
            if st.playing_on():
                self.audio_btn.add_css_class("suggested-action")
            else:
                self.audio_btn.remove_css_class("suggested-action")
            if st.streaming():
                self.mirror_btn.add_css_class("suggested-action")
                self.mirror_btn.set_tooltip_text("Mirroring")
            else:
                self.mirror_btn.remove_css_class("suggested-action")
                self.mirror_btn.set_tooltip_text("Mirror screen (AirPlay)")
            self.visible_label.set_label(st.headline())
            for key, pill in self.pills.items():
                if key in st.protocols:
                    pill.add_css_class("on")
                else:
                    pill.remove_css_class("on")
            if services.airdrop_on():
                left = services.minutes_left()
                self.banner.set_title("AirDrop is on – Apple devices can see this computer"
                                      + (f" ({left} min left)" if left is not None else ""))
                self.banner.set_button_label("Turn off")
            else:
                self.banner.set_title("AirDrop is off – Quick Share and LocalSend are on")
                self.banner.set_button_label("Turn AirDrop on")
            self.banner.set_revealed(True)

            # Always offered: works even when a phone does not show up in the search.
            shown = st.devices + ([QR_DEVICE] if "quickshare" in st.protocols else [])
            keys = [d["name"] for d in shown]
            if keys != list(self.bubbles):
                clear_box(self.flow)
                old = self.bubbles
                self.bubbles = {}
                for d in shown:
                    b = DeviceBubble(self.app, d)
                    if d["name"] in old and old[d["name"]].progress.get_visible():
                        b.progress.set_visible(True)
                        b.progress.set_fraction(old[d["name"]].progress.get_fraction())
                    self.bubbles[d["name"]] = b
                    self.flow.append(b)
            self.empty.set_visible(not st.devices)
            self.empty.set_label("Looking for devices …" if st.searching or not st.connected
                                 else "No devices nearby yet.\nAndroid: Quick Share on “Everyone” · Samsung: "
                                      "QR code tile\nLocalSend: keep the app open · iPhone/Mac: "
                                      "turn AirDrop on")

    # --- application --------------------------------------------------------

    class MultidropApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
            self.state = State()
            self.window: Window | None = None
            self.tray: Tray | None = None
            self.jobs: dict[str, str] = {}      # job id -> device name
            self.offer_dialogs: dict[str, Dialog] = {}
            self.last_search = 0.0
            self.quitting = False
            self.minutes_left: int | None = None
            self.pin_asked: set[str] = set()
            self.airplay_caps: dict[str, dict] = {}
            self.device_seen: dict[str, tuple[float, dict]] = {}   # name -> (time, group)
            self.qr_dialog = None               # (job id, Dialog) while a QR code is shown

        def do_startup(self) -> None:
            Adw.Application.do_startup(self)
            css = Gtk.CssProvider()
            load_css(css)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.hold()                         # lives on in the top bar
            try:
                self.tray = Tray(self)
            except GLib.Error as e:
                print(f"top bar icon unavailable: {e.message}", file=sys.stderr)
            if not tray_host():
                print("no StatusNotifier host: on GNOME install the extension "
                      "'AppIndicator and KStatusNotifierItem Support' for the top bar icon",
                      file=sys.stderr)
                args.background = False     # nothing to show it in
            services.ensure_daemon()
            GLib.timeout_add_seconds(1, self.tick)
            threading.Thread(target=self.event_loop, daemon=True).start()
            if self.state.mirror_ok:
                threading.Thread(target=self.mirror_loop, daemon=True).start()
            if self.state.audio_ok:
                threading.Thread(target=self.audio_loop, daemon=True).start()

        def do_activate(self) -> None:
            if not args.background or self.window is not None:
                self.show_window()
            args.background = False

        def do_open(self, files, _n, _hint) -> None:
            self.show_window()
            self.window.set_pending([f.get_path() for f in files if f.get_path()])
            self.refresh_devices()

        def show_window(self) -> None:
            if self.window is None:
                self.window = Window(self)
                if shot := os.environ.get("MULTIDROP_SNAPSHOT"):    # development aid
                    GLib.timeout_add_seconds(8, self.snapshot, shot)
            self.window.present()
            if time.time() - self.last_search > 15:
                self.refresh_devices()

        def snapshot(self, path: str) -> bool:
            win = self.window
            paint = Gtk.WidgetPaintable(widget=win)
            snap = Gtk.Snapshot()
            paint.snapshot(snap, win.get_width(), win.get_height())
            node = snap.to_node()
            if node is not None:
                win.get_renderer().render_texture(node, None).save_to_png(path)
            return False

        def changed(self) -> None:
            if self.window is not None:
                self.window.update()
            if self.tray is not None:
                self.tray.rebuild()

        def toast(self, text: str, button=None, action=None) -> None:
            if self.window is not None and self.window.get_visible():
                self.window.toast(text, button, action)
            else:
                n = Gio.Notification.new(APP_NAME)
                n.set_body(text)
                self.send_notification(None, n)

        # daemon state

        def tick(self) -> bool:
            code = services.poll_session()
            if code is not None:
                if self.quitting:
                    services.ensure_daemon()
                    self.release()
                    self.quit()
                    return False
                if code not in (0, -signal.SIGTERM):
                    self.airdrop_failed()
                else:
                    self.toast("AirDrop is off again")
                self.changed()
            if not self.quitting:
                services.ensure_daemon()
            # Keep looking while the window is open: phones answer the BLE
            # wake-up with mDNS only after a few seconds, and only while visible.
            if self.window is not None and self.window.get_visible() \
                    and time.time() - self.last_search > 12:
                self.refresh_devices()
            left = services.minutes_left()
            if left != self.minutes_left:
                self.minutes_left = left
                self.changed()
            return True

        def event_loop(self) -> None:
            """Follow the daemon's events; reconnects when it restarts."""
            since, pid = 0, None
            while True:
                now_pid = services.pid()
                if now_pid is None:
                    if self.state.connected:
                        GLib.idle_add(self.on_disconnected)
                    time.sleep(1)
                    continue
                if now_pid != pid:
                    try:
                        status = api.request("GET", "/api/status")
                    except ApiError:
                        time.sleep(1)
                        continue
                    since, pid = 0, now_pid
                    connected_at = time.time()
                    GLib.idle_add(self.on_status, status)
                try:
                    for ev in api.events(since):
                        since = max(since, ev.get("seq", 0))
                        if ev.get("time", 0) >= connected_at - 2:   # not the replayed history
                            GLib.idle_add(self.on_event, ev)
                except (ApiError, OSError):
                    pass
                time.sleep(0.5)

        def on_disconnected(self) -> bool:
            self.state.connected = False
            self.state.protocols = set()
            self.changed()
            return False

        def on_status(self, status: dict) -> bool:
            st = self.state
            st.connected = True
            st.alias = status.get("alias", "")
            st.download_dir = status.get("download_dir", st.download_dir)
            st.protocols = set(status.get("protocols", {}))
            self.changed()
            self.refresh_devices()
            return False

        def refresh_devices(self) -> None:
            if self.state.searching or not self.state.connected:
                return
            self.state.searching = True
            self.last_search = time.time()
            self.changed()
            timeout = 12 if "airdrop" in self.state.protocols else 8

            def done(result, error):
                self.state.searching = False
                if error is None:   # our own Quick Share/LocalSend advert is not a peer
                    now = time.time()
                    for d in result:
                        local = all(str(t.get("address", "")).startswith("127.")
                                    for t in d["targets"])
                        if d["name"] != self.state.alias and not local:
                            self.device_seen[d["name"]] = (now, d)
                    # Keep what a restarted daemon (AirDrop on/off) has not found yet.
                    self.device_seen = {n: v for n, v in self.device_seen.items()
                                        if now - v[0] <= DEVICE_TTL}
                    self.state.devices = sorted((v[1] for v in self.device_seen.values()),
                                                key=lambda d: d["name"].lower())
                self.changed()
            in_thread(lambda: api.request("GET", f"/api/devices?refresh=1&timeout={timeout}",
                                          timeout=timeout + 15), done)

        # sending

        def pick_and_send(self, device: dict) -> None:
            if self.window is not None and self.window.pending:
                paths = self.window.pending
                self.window.set_pending([])
                self.send(device, paths)
                return
            parent = self.window if self.window and self.window.get_visible() else None
            pick_files(parent, f"Send to {device['name']}",
                       lambda paths: self.send(device, paths))

        def send(self, device: dict, paths: list[str]) -> None:
            target = {"qr": True} if device.get("qr") else {"key": device["targets"][0]["key"]}

            def request():
                try:
                    return api.request("POST", "/api/send", {"target": target, "paths": paths},
                                       timeout=60)
                except ApiError as e:
                    # A daemon restarted since the search (AirDrop on/off) does not
                    # know the key yet; Quick Share and LocalSend work by address.
                    t = device["targets"][0]
                    if "unknown device" not in str(e) or t.get("protocol") == "airdrop" \
                            or not t.get("address"):
                        raise
                    return api.request("POST", "/api/send", {
                        "target": {"protocol": t["protocol"], "address": t["address"],
                                   "port": t.get("port")}, "paths": paths}, timeout=60)

            def done(result, error):
                if error is not None:
                    self.toast(f"Sending to {device['name']} failed: {error}")
                else:
                    self.jobs[result["job"]] = device["name"]
                    self.set_progress(device["name"], 0.0)
            in_thread(request, done)

        def set_progress(self, name: str, fraction: float | None) -> None:
            if self.window is None or name not in self.window.bubbles:
                return
            bar = self.window.bubbles[name].progress
            bar.set_visible(fraction is not None)
            if fraction is not None:
                bar.set_fraction(fraction)

        # events

        def on_event(self, ev: dict) -> bool:
            kind = ev.get("event")
            if kind == "started":
                self.state.protocols = set(ev.get("protocols", {}))
                self.changed()
            elif kind == "progress" and ev.get("job") in self.jobs:
                size = ev.get("size") or 0
                self.set_progress(self.jobs[ev["job"]],
                                  min(1.0, ev.get("done", 0) / size) if size else 0.0)
            elif kind == "qr" and ev.get("job") in self.jobs:
                self.show_qr(ev["job"], ev.get("svg") or "")
            elif kind == "qr_scanned":
                self.close_qr()
                self.toast(f"QR code scanned by {ev.get('device') or 'phone'}")
            elif kind in ("job_done", "job_failed", "job_cancelled"):
                if self.qr_dialog is not None and self.qr_dialog[0] == ev.get("job"):
                    self.close_qr()
                name = self.jobs.pop(ev.get("job"), None) or ev.get("info", {}).get("to", "?")
                self.set_progress(name, None)
                if kind == "job_done":
                    self.toast(f"Sent to {name}")
                elif kind == "job_failed":
                    self.toast(f"Sending to {name} failed: {ev.get('error', '')[:160]}")
            elif kind == "offer":
                self.ask(ev["offer"])
            elif kind in ("offer_accepted", "offer_rejected", "offer_expired", "offer_withdrawn"):
                dlg = self.offer_dialogs.pop(ev.get("id") or ev.get("offer", {}).get("id"), None)
                if dlg is not None:
                    dlg.close()
            elif kind == "file_received":
                path = ev.get("path", "")
                self.toast(f"Received {ev.get('file')} from {ev.get('from')}", "Show",
                           lambda: open_path(path, containing=True))
            elif kind == "message":
                text = ev.get("text") or ""
                if ev.get("kind") == "url" or text.startswith(("http://", "https://")):
                    self.toast(f"Link from {ev.get('from')}: {text[:80]}", "Open",
                               lambda: launch_uri(text))
                elif text:
                    self.toast(f"Text from {ev.get('from')}: {text[:120]}", "Copy",
                               lambda: Gdk.Display.get_default().get_clipboard().set(text))
            return False

        def show_qr(self, job: str, svg: str) -> None:
            self.close_qr()
            picture = Gtk.Picture(can_shrink=True, width_request=260, height_request=260)
            try:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(svg.encode()))
                picture.set_paintable(texture)
            except GLib.Error as e:
                print(f"QR code: {e.message}", file=sys.stderr)
            frame = Gtk.Box(halign=Gtk.Align.CENTER)
            frame.add_css_class("qr")
            frame.append(picture)

            def answered(response):
                if self.qr_dialog is not None and self.qr_dialog[0] == job:
                    self.qr_dialog = None
                    if response == "cancel":
                        in_thread(lambda: api.request("POST", f"/api/jobs/{job}/cancel"))
            self.show_window()
            dlg = Dialog(self.window, "Scan with your Android phone",
                         "Open the camera or Quick Share on the phone and scan this code. "
                         "The transfer then starts without searching.",
                         [("cancel", "Cancel")], None, answered, extra=frame)
            self.qr_dialog = (job, dlg)
            dlg.present()

        def close_qr(self) -> None:
            if self.qr_dialog is not None:
                _job, dlg = self.qr_dialog
                self.qr_dialog = None
                dlg.close()

        def ask(self, offer: dict) -> None:
            # Desktop notifications from multidropd cover the case without window.
            if self.window is None or not self.window.is_active():
                return
            files = offer.get("files", [])
            what = files[0]["name"] if len(files) == 1 else f"{len(files)} items"
            body = f"{what} ({human_size(offer.get('total_size'))}) via " \
                   f"{PROTO.get(offer['protocol'], offer['protocol'])}"
            if offer.get("pin"):
                body += f"\nCode: {offer['pin']}"
            def answered(response):
                if self.offer_dialogs.pop(offer["id"], None) is None:
                    return
                in_thread(lambda: api.request("POST", f"/api/offers/{offer['id']}",
                                              {"accept": response == "yes"}))
            dlg = Dialog(self.window, f"{offer['from']} wants to send you something", body,
                         [("no", "Decline"), ("yes", "Accept")], "yes", answered)
            self.offer_dialogs[offer["id"]] = dlg
            dlg.present()

        # AirDrop

        def airdrop_on(self, minutes: int) -> None:
            """Ask first: the session may move the Wi-Fi to another access point."""
            self.show_window()

            def answered(response):
                if response == "on":
                    self.start_airdrop(minutes)
            Dialog(self.window, f"Turn AirDrop on for {minutes} minutes?",
                   "iPhones and Macs nearby can then see this computer.\n\n"
                   "If your Wi-Fi runs on a channel AirDrop cannot share (e.g. 6 GHz), "
                   f"{APP_NAME} moves to the same network on channel 6, 44 or 149 for this time "
                   "– the connection drops briefly – and back automatically afterwards. "
                   "Nothing needs to change on the router.\n\nYou will be asked for your password.",
                   [("cancel", "Cancel"), ("on", "Turn on")], "on", answered).present()

        def start_airdrop(self, minutes: int) -> None:
            def done(_result, error):
                if error is not None:
                    self.toast(f"AirDrop could not be started: {error}")
                else:
                    self.toast("AirDrop is starting – please enter your password")
                self.changed()
            in_thread(lambda: services.start_airdrop(minutes), done)

        def airdrop_failed(self) -> None:
            lines = log_tail(services.logs / "desktop-airdrop.log", 6).splitlines()
            errors = [ln.removeprefix("error: ") for ln in lines if ln.startswith("error")]
            detail = errors[-1] if errors else (lines[-1] if lines else "unknown error")
            if "MHz" in detail and "channel" in detail:
                detail = ("Your Wi-Fi runs on a channel AirDrop cannot share, and the same network "
                          "is not available on channel 6, 44 or 149.\n\n"
                          + detail)
            self.show_window()
            Dialog(self.window, "AirDrop could not start", detail, [("ok", "OK")]).present()

        def airdrop_off(self) -> None:
            services.stop_airdrop()
            self.toast("Turning AirDrop off …")

        # AirPlay audio

        def audio_loop(self) -> None:
            while True:
                try:
                    speakers.ensure(services.logs)
                    sinks, default = speakers.state()
                    GLib.idle_add(self.on_audio, sinks, default)
                except (OSError, ValueError, subprocess.SubprocessError) as e:
                    print(f"AirPlay audio: {e}", file=sys.stderr)
                    time.sleep(10)
                time.sleep(3)

        def on_audio(self, sinks: list, default: str) -> bool:
            st = self.state
            if (sinks, default) != (st.speakers, st.audio_default):
                st.speakers, st.audio_default = sinks, default
                self.changed()
            return False

        def audio_on(self, sink: dict) -> None:
            def done(_r, error):
                self.toast(f"Audio now plays on {sink['name']}" if error is None
                           else f"Could not select {sink['name']}: {error}")
                if error is None:
                    self.state.audio_default = sink["node"]
                    self.changed()
            in_thread(lambda: speakers.play_on(sink), done)

        def audio_pc(self) -> None:
            def done(_r, error):
                self.toast("Audio back on this computer" if error is None
                           else f"Switching back failed: {error}")
            in_thread(speakers.back_to_pc, done)

        def audio_volume(self, sink_id: int, value: float) -> None:
            # Coalesce slider drags: only the last value of a burst is sent.
            self.pending_volume = (sink_id, value)
            if not getattr(self, "volume_timer", 0):
                def send():
                    self.volume_timer = 0
                    sid, val = self.pending_volume
                    in_thread(lambda: speakers.volume(sid, val))
                    return False
                self.volume_timer = GLib.timeout_add(150, send)

        # AirPlay screen mirroring

        def mirror_loop(self) -> None:
            """Keep doubletake running and follow its devices and streams."""
            caps: dict[str, dict] = {}
            caps_at = 0.0
            while True:
                try:
                    if time.time() - caps_at > 30 and services.pid() is not None:
                        try:
                            found = api.request("GET", "/api/airplay?timeout=8", timeout=20)
                            caps = {k: d for d in found for k in (d["device_id"], d["ip"]) if k}
                            caps_at = time.time()
                        except ApiError:
                            pass
                    self.airplay_caps = caps
                    mirror.ensure(services.logs)
                    devices = mirror.request("devices").get("devices") or []
                    status = mirror.request("status")
                    GLib.idle_add(self.on_mirror, devices, status.get("streams") or [], "")
                except ApiError as e:
                    GLib.idle_add(self.on_mirror, [], [], str(e))
                    time.sleep(8)
                time.sleep(2)

        def on_mirror(self, devices: list, streams: list, error: str) -> bool:
            st = self.state
            screens = sorted((d for d in devices if is_mirror_target(d, self.airplay_caps)),
                             key=lambda d: d["name"].lower())
            before = (st.screens, st.streams, st.mirror_error)
            st.screens, st.streams, st.mirror_error = screens, streams, error
            for s in streams:
                if s.get("state") == "pin_required" and s.get("device_ip") not in self.pin_asked:
                    self.pin_asked.add(s.get("device_ip"))
                    self.ask_pin(s)
            gone = {ip for ip in self.pin_asked if ip not in {s.get("device_ip") for s in streams}}
            self.pin_asked -= gone
            if (st.screens, st.streams, st.mirror_error) != before:
                self.changed()
            return False

        def mirror_start(self, device: dict) -> None:
            def done(result, error):
                if error is not None or not result.get("ok"):
                    self.toast(f"Mirroring to {device['name']} failed: "
                               f"{error or result.get('error')}")
                else:
                    self.toast(f"Mirroring to {device['name']} – pick the screen "
                               "in the system dialog if asked")
            in_thread(lambda: mirror.request("connect", target=device["ip"],
                                             port=device.get("port")), done)

        def mirror_stop(self, ip: str | None) -> None:
            in_thread(lambda: mirror.request("disconnect", target=ip),
                      lambda r, e: self.toast("Mirroring stopped" if e is None and r.get("ok")
                                              else f"Stopping failed: {e or r.get('error')}"))

        def ask_pin(self, stream: dict) -> None:
            ip = stream.get("device_ip")
            name = stream.get("device") or ip
            password = stream.get("credential_kind") == "password"
            entry = Gtk.PasswordEntry(show_peek_icon=True) if password else \
                Gtk.Entry(input_purpose=Gtk.InputPurpose.DIGITS, max_length=8)
            entry.set_activates_default(True)

            def answered(response):
                code = entry.get_text().strip()
                if response == "ok" and code:
                    in_thread(lambda: mirror.request("connect", target=ip, pin=code),
                              lambda r, e: None if e is None and r.get("ok") else
                              self.toast(f"Code rejected: {e or r.get('error')}"))
                else:
                    self.mirror_stop(ip)
            self.show_window()
            Dialog(self.window, f"Pair with {name}",
                   "Enter the device's password." if password else
                   "Enter the code shown on the TV.",
                   [("cancel", "Cancel"), ("ok", "Connect")], "ok", answered,
                   extra=entry).present()
            entry.grab_focus()

        def quit_all(self) -> None:
            """End an AirDrop session first (restores Wi-Fi); multidropd stays."""
            mirror.stop()
            speakers.stop()
            if services.session is not None and services.session.poll() is None:
                self.quitting = True
                services.stop_airdrop()
                if self.window is not None:
                    self.window.set_visible(False)
                return                          # tick() quits once it is gone
            self.release()
            self.quit()

    GLib.set_prgname(APP_ID)        # X11 WM_CLASS = StartupWMClass of the .desktop file
    GLib.set_application_name(APP_NAME)
    app = MultidropApp()
    argv0 = [sys.argv[0]]
    files = [str(Path(f).resolve()) for f in args.files]
    return app.run(argv0 + files)


if __name__ == "__main__":
    sys.exit(main())
