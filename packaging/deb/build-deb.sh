#!/bin/bash
# Build multidrop_<version>_all.deb (Ubuntu 24.04+, Debian 13+).
# Uses the distribution's Python libraries; only protobuf (pure Python, the
# distributions ship versions too old for the generated code) is bundled.
#
#   packaging/deb/build-deb.sh [OUTDIR]      (default: dist/)

set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=${1:-$ROOT/dist}
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")
ID=org.multidrop.Multidrop
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
P=$WORK/multidrop_${VERSION}_all
LIB=$P/usr/lib/multidrop

mkdir -p "$LIB/vendor" "$P/usr/bin" "$P/DEBIAN" "$P/usr/share/applications" \
         "$P/etc/xdg/autostart" "$P/usr/share/icons/hicolor/scalable/apps" \
         "$P/usr/share/icons/hicolor/symbolic/apps" "$P/usr/share/kio/servicemenus" \
         "$P/usr/share/nemo/actions" "$P/usr/lib/systemd/user" "$P/usr/share/doc/multidrop"

# Python package (no tests, no caches) and the bundled pure-Python protobuf.
(cd "$ROOT/src" && find multidrop -name '__pycache__' -prune -o -type f -print \
   | grep -v '\.egg-info' | cpio -pdm --quiet "$LIB")
# Bundled, pure Python (no compiled parts, so one package fits every
# architecture and Python >= 3.11): protobuf, because distributions ship versions
# too old for the generated code; aiohttp and its helpers, because the AirDrop
# side needs aiohttp >= 3.10 (Ubuntu 24.04 has 3.9).
export AIOHTTP_NO_EXTENSIONS=1 MULTIDICT_NO_EXTENSIONS=1 YARL_NO_EXTENSIONS=1 \
       FROZENLIST_NO_EXTENSIONS=1 PROPCACHE_NO_EXTENSIONS=1
python3 -m pip install --quiet --no-deps --no-compile --target "$LIB/vendor" \
        --no-binary aiohttp,multidict,yarl,frozenlist,propcache \
        "aiohttp==${AIOHTTP:-3.14.5}" "multidict==7.1.0" \
        "yarl==1.25.1" "frozenlist==1.8.0" "propcache==0.5.4" "aiosignal==1.4.0" \
        "aiohappyeyeballs==2.7.1"
python3 -m pip download --quiet --no-deps --only-binary=:all: --platform any \
        --dest "$WORK/wheels" "protobuf==${PROTOBUF:-7.36.2}"        # py3-none-any
python3 -m zipfile -e "$WORK"/wheels/protobuf-*-py3-none-any.whl "$LIB/vendor"
if find "$LIB/vendor" -name '*.so' | grep -q .; then
  echo "compiled module in vendor/, the package must stay pure Python" >&2; exit 1
fi
find "$LIB/vendor" \( -name RECORD -o -name INSTALLER -o -name REQUESTED -o -name '__pycache__' \) \
     -prune -exec rm -rf {} +
find "$LIB/vendor" -name 'direct_url.json' -delete

launcher() {  # name module
  cat > "$P/usr/bin/$1" <<PY
#!/usr/bin/python3
import sys
sys.path[:0] = ["/usr/lib/multidrop", "/usr/lib/multidrop/vendor"]
from $2 import main
sys.exit(main())
PY
  chmod 755 "$P/usr/bin/$1"
}
launcher multidrop multidrop.cli
launcher multidropd multidrop.daemon
cat > "$P/usr/bin/multidrop-desktop" <<'SH2'
#!/bin/sh
# GTK app: system Python with gi; it starts /usr/bin/multidropd itself.
exec /usr/bin/python3 /usr/lib/multidrop/multidrop/desktop.py "$@"
SH2
chmod 755 "$P/usr/bin/multidrop-desktop"
install -m 755 "$ROOT/tools/fetch-doubletake.sh" "$P/usr/bin/multidrop-fetch-doubletake"

entry() {  # extra Exec arguments
  cat <<DESKTOP
[Desktop Entry]
Type=Application
Name=MultiDrop
GenericName=Nearby sharing
Comment=AirDrop, Quick Share, LocalSend and AirPlay on Linux
Exec=multidrop-desktop $1
Icon=$ID
Terminal=false
Categories=Network;FileTransfer;
Keywords=AirDrop;AirPlay;Quick Share;Nearby;LocalSend;share;send;
StartupNotify=true
StartupWMClass=$ID
DESKTOP
}
{ entry "%F"; echo "MimeType=application/octet-stream;inode/directory;"; } \
  > "$P/usr/share/applications/$ID.desktop"
{ entry "--background"; echo "NoDisplay=true"; echo "X-GNOME-Autostart-enabled=true"; } \
  > "$P/etc/xdg/autostart/$ID.desktop"
install -m 644 "$ROOT/data/icons/multidrop.svg" "$P/usr/share/icons/hicolor/scalable/apps/$ID.svg"
install -m 644 "$ROOT/data/icons/multidrop-symbolic.svg" \
        "$P/usr/share/icons/hicolor/symbolic/apps/$ID-symbolic.svg"
cat > "$P/usr/share/kio/servicemenus/multidrop.desktop" <<EOF
[Desktop Entry]
Type=Service
MimeType=application/octet-stream;inode/directory;
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
Actions=send

[Desktop Action send]
Name=Send with MultiDrop
Icon=$ID
Exec=multidrop-desktop %F
EOF
chmod 755 "$P/usr/share/kio/servicemenus/multidrop.desktop"
cat > "$P/usr/share/nemo/actions/multidrop.nemo_action" <<EOF
[Nemo Action]
Name=Send with MultiDrop
Comment=Send to a nearby device
Exec=multidrop-desktop %F
Icon-Name=$ID
Selection=notnone
Extensions=any;
EOF
sed 's|^ExecStart=.*|ExecStart=/usr/bin/multidropd|; /^#.*cp \|^#   systemctl\|^# ExecStart assumes/d' \
    "$ROOT/data/systemd/multidropd.service" > "$P/usr/lib/systemd/user/multidropd.service"
cat > "$P/usr/share/doc/multidrop/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: MultiDrop
Source: https://github.com/digitaldanis/multidrop

Files: *
License: GPL-3.0-or-later
 See /usr/share/common-licenses/GPL-3.

Files: usr/lib/multidrop/vendor/*
License: BSD-3-Clause and Apache-2.0
 Protocol Buffers (BSD-3-Clause, Google LLC); aiohttp, multidict, yarl,
 frozenlist, propcache, aiosignal, aiohappyeyeballs (Apache-2.0, aio-libs).
 License files are kept in the *.dist-info directories.

Comment: Third-party notices:
 https://github.com/digitaldanis/multidrop/blob/main/THIRD_PARTY_NOTICES.md
EOF

SIZE=$(du -sk "$P" | cut -f1)
cat > "$P/DEBIAN/control" <<EOF
Package: multidrop
Version: $VERSION
Architecture: all
Maintainer: digitaldanis <336468140+digitaldanis@users.noreply.github.com>
Installed-Size: $SIZE
Section: net
Priority: optional
Homepage: https://github.com/digitaldanis/multidrop
Depends: python3 (>= 3.11), python3-cryptography, python3-zeroconf (>= 0.130),
 python3-dbus-fast, python3-segno, python3-attr, python3-idna,
 python3-typing-extensions, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1
Recommends: network-manager, pkexec | policykit-1, curl,
 gnome-shell-extension-appindicator | plasma-workspace, pipewire-bin, wireplumber,
 gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad,
 gstreamer1.0-pipewire, gstreamer1.0-tools
Description: AirDrop, Quick Share, LocalSend and AirPlay for Linux
 Share files, your screen and audio with phones, Macs and TVs nearby:
 Quick Share (Android, Samsung), LocalSend, browser download via QR code,
 AirDrop (supported Wi-Fi cards with omdrop-owl), AirPlay screen mirroring
 (doubletake, fetched with multidrop-fetch-doubletake) and AirPlay audio
 (PipeWire). GTK desktop app with a top bar menu, CLI and background service.
EOF

mkdir -p "$OUT"
find "$P" -type d -exec chmod 755 {} +
dpkg-deb --root-owner-group --build "$P" "$OUT/multidrop_${VERSION}_all.deb" >/dev/null
echo "$OUT/multidrop_${VERSION}_all.deb"
