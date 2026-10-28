#!/usr/bin/env python3
"""Interop check against the official LocalSend CLI (localsend-cli, v1.18.x).

Drives localsend-cli through a pseudo terminal and multidrop through its CLI.
Discovery uses the real LAN (multicast on the default interface); transfers
go over 127.0.0.1. Usage:

    .venv/bin/python tools/dev/localsend_interop.py ~/multidrop-research/localsend-app/cli/localsend-cli
"""

import fcntl
import hashlib
import os
import pty
import re
import select
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

MULTIDROP = [sys.executable, "-m", "multidrop"]
# localsend-cli listens on the interface addresses only, not on 127.0.0.1.
HOST = None
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")
LS_PORT, MD_PORT = 53320, 53321
results: list[tuple[str, bool, str]] = []


class Pty:
    def __init__(self, argv, env):
        self.master, slave = pty.openpty()
        # The send mode is a full-screen TUI; at 0x0 it draws nothing.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                     env=env, close_fds=True)
        os.close(slave)
        self.text = ""

    def _read(self, timeout):
        r, _, _ = select.select([self.master], [], [], timeout)
        if r:
            try:
                self.text += ANSI.sub(b"", os.read(self.master, 65536)).decode(errors="replace")
            except OSError:
                pass

    def expect(self, pattern, timeout=20.0, start=0):
        end = time.time() + timeout
        while time.time() < end:
            m = re.search(pattern, self.text[start:])
            if m:
                return m
            self._read(0.2)
        raise TimeoutError(f"{pattern!r} not seen; output so far:\n{self.text[-2000:]}")

    def send(self, keys: str):
        os.write(self.master, keys.encode())

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)


def multidrop(args, env, timeout=60, background=False):
    argv = MULTIDROP + ["--alias", "multidrop-interop", "--no-ble",
                        "--mdns-address", "127.0.0.1"] + args
    if background:
        return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)


def main(cli: str) -> int:
    global HOST
    from multidrop.backends.quickshare.mdns import default_address
    HOST = default_address()
    work = Path(tempfile.mkdtemp(prefix="ls-interop-"))
    ls_in, md_in = work / "ls-in", work / "md-in"
    ls_in.mkdir()
    env_ls = {**os.environ, "XDG_CONFIG_HOME": str(work / "ls-cfg"), "TERM": "xterm"}
    env_md = {**os.environ, "XDG_DATA_HOME": str(work / "md-data"),
              "MULTIDROP_CONFIG": str(work / "none.toml")}
    photo = work / "Urlaub Foto.jpg"
    photo.write_bytes(os.urandom(4_000_000))
    note = work / "note.txt"
    note.write_text("Hello from the official app\n")

    ls_argv = [cli, "--alias", "LS-Official-CLI", "--port", str(LS_PORT),
               "--destination", str(ls_in)]

    # -- official CLI receives ------------------------------------------
    ls = Pty(ls_argv, env_ls)
    try:
        ls.expect(r"Ready to accept requests")
        r = multidrop(["devices", "--via", "localsend", "--timeout", "4"], env_md)
        check("multidrop devices finds official CLI (LAN multicast)",
              "LS-Official-CLI" in r.stdout, r.stdout.strip().replace("\n", " | "))

        # Our fingerprint assumption: SHA-256 over the DER certificate, uppercase.
        pem = (work / "ls-cfg/localsend-cli/identity.pem").read_text()
        cert_pem = re.search(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
                             pem, re.S).group(0)
        fp = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert_pem)).hexdigest().upper()
        info = subprocess.run(["curl", "-sk", f"https://{HOST}:{LS_PORT}/api/localsend/v2/info", "--cert", str(work / "md-data/multidrop/localsend/cert.pem"), "--key", str(work / "md-data/multidrop/localsend/key.pem")],
                              capture_output=True, text=True).stdout
        check("fingerprint = SHA-256(DER) uppercase", fp in info, info[:200])

        # Send by name: discovery + certificate pinning against the announced fingerprint.
        mark = len(ls.text)
        p = multidrop(["send", str(photo), str(note), "--to", "LS-Official-CLI",
                       "--via", "localsend", "--timeout", "4"], env_md, background=True)
        ls.expect(r"(?i)accept|\[y/n", start=mark, timeout=30)
        ls.send("y")
        out, _ = p.communicate(timeout=60)
        ok = (p.returncode == 0 and (ls_in / photo.name).exists()
              and (ls_in / photo.name).read_bytes() == photo.read_bytes()
              and (ls_in / note.name).read_text() == note.read_text())
        check("multidrop -> official CLI (2 files, by name, pinned TLS)", ok,
              out.strip().splitlines()[-1] if out.strip() else "")

        mark = len(ls.text)
        r = multidrop(["send", "--text", "Text message äöü", "--to", f"{HOST}:{LS_PORT}",
                       "--via", "localsend"], env_md)
        try:
            ls.expect(r"Message received\s+Text message äöü", start=mark, timeout=10)
            seen = True
        except TimeoutError:
            seen = False
        check("multidrop -> official CLI (text message)", r.returncode == 0 and seen,
              f"exit={r.returncode} shown={seen}")

        mark = len(ls.text)
        p = multidrop(["send", str(note), "--to", f"{HOST}:{LS_PORT}", "--via", "localsend"],
                      env_md, background=True)
        ls.expect(r"(?i)accept|\[y/n", start=mark, timeout=30)
        ls.send("n")
        out, _ = p.communicate(timeout=30)
        check("official CLI declines -> multidrop reports it",
              p.returncode == 1 and "rejected" in out, out.strip().splitlines()[-1])
    finally:
        ls.close()

    # -- official CLI sends ---------------------------------------------
    rcv = multidrop(["--port", str(MD_PORT), "receive", "--via", "localsend",
                     "--accept", "all", "--once", "--dir", str(md_in)], env_md, background=True)
    time.sleep(1.5)
    ls = Pty(ls_argv + ["-f", str(photo), "-f", str(note)], env_ls)
    try:
        # Full-screen device list: the first discovered entry is selected.
        ls.expect(r"\[1\] multidrop-interop", timeout=20)
        ls.send("\r")
        out, _ = rcv.communicate(timeout=60)
        ok = (rcv.returncode == 0 and (md_in / photo.name).read_bytes() == photo.read_bytes()
              and (md_in / note.name).read_text() == note.read_text())
        check("official CLI -> multidrop (2 files, discovered via multicast)", ok,
              " | ".join(out.strip().splitlines()[-3:]))
    except Exception as e:
        rcv.kill()
        check("official CLI -> multidrop", False, str(e)[-600:])
    finally:
        ls.close()
        if rcv.poll() is None:
            rcv.kill()

    shutil.rmtree(work, ignore_errors=True)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
