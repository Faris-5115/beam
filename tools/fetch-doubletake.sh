#!/bin/bash
# Fetch doubletake (AirPlay screen mirroring sender for Linux, LGPL-3.0,
# https://github.com/omarroth/doubletake) for the desktop app's "Bildschirm
# spiegeln". Pinned release, SHA-256 checked, installed for the current user
# only (no root): ~/.local/lib/multidrop/doubletake/.
#
#   tools/fetch-doubletake.sh           download the pinned release binaries
#   tools/fetch-doubletake.sh --build   build the same tag from source instead (needs Go >= 1.25)
#
# Runtime: GStreamer 1.0 with base/good/bad(/ugly) plugins and libav,
# PipeWire + xdg-desktop-portal (Wayland) or X11; pactl for audio (optional).

set -euo pipefail

VERSION=v0.5.0
REV=e18f85d                     # tag v0.5.0, 2026-10-03
DEST=$HOME/.local/lib/multidrop/doubletake
URL=https://github.com/omarroth/doubletake/releases/download/$VERSION

case $(uname -m) in
  x86_64)
    SUM_DT=59027455bb844f15119232600569aded721203271001591a0afc5a0d07c06b5c
    SUM_CTL=1cf40195f83ef4c1f885392e1b90d2ce077270b9391a38a717598170cb1aa4eb
    SUFFIX="" ;;
  aarch64|arm64)
    SUM_DT=bcc5f11e499673f7c86f100531f3c5a8e607626cdf50b9d97822a8f39dd0d5b7
    SUM_CTL=74f0fd84dc3f628229b0036c69d28534f951204c487f7d9cb2004e33aaa89e33
    SUFFIX=-arm64 ;;
  *) echo "no doubletake release for $(uname -m); use --build" >&2; exit 1 ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$DEST"

if [ "${1:-}" = --build ]; then
  command -v go >/dev/null || { echo "go is not installed" >&2; exit 1; }
  git clone -q https://github.com/omarroth/doubletake.git "$tmp/src"
  git -C "$tmp/src" -c advice.detachedHead=false checkout -q "$REV"
  (cd "$tmp/src" && CGO_ENABLED=0 go build -trimpath -o "$tmp/doubletake" ./cmd/doubletake \
                 && CGO_ENABLED=0 go build -trimpath -o "$tmp/doubletake-ctl" ./cmd/doubletake-ctl)
else
  fetch() {  # asset sha256 out
    curl -fsSL --retry 3 -o "$3" "$URL/$1"
    echo "$2  $3" | sha256sum -c --quiet - || { echo "checksum mismatch: $1" >&2; exit 1; }
  }
  fetch "doubletake$SUFFIX" "$SUM_DT" "$tmp/doubletake"
  fetch "doubletake-ctl$SUFFIX" "$SUM_CTL" "$tmp/doubletake-ctl"
fi
install -m 755 "$tmp/doubletake" "$tmp/doubletake-ctl" "$DEST/"
echo "$VERSION ${1:-release}" > "$DEST/VERSION"
echo "installed doubletake $VERSION to $DEST"

# Runtime check (GStreamer elements doubletake uses for capture and H.264).
missing=()
command -v gst-inspect-1.0 >/dev/null || missing+=("GStreamer tools")
if command -v gst-inspect-1.0 >/dev/null; then
  gst-inspect-1.0 videoconvert >/dev/null 2>&1 || missing+=("gst-plugins-base")
  gst-inspect-1.0 rtpbin >/dev/null 2>&1 || missing+=("gst-plugins-good")
  { gst-inspect-1.0 openh264enc || gst-inspect-1.0 x264enc || gst-inspect-1.0 vah264enc \
    || gst-inspect-1.0 nvh264enc; } >/dev/null 2>&1 || missing+=("an H.264 encoder (gst-plugins-bad/ugly)")
  if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    gst-inspect-1.0 pipewiresrc >/dev/null 2>&1 || missing+=("GStreamer PipeWire plugin")
  fi
fi
command -v pactl >/dev/null || echo "note: pactl missing, mirroring runs without sound (pulseaudio-utils / libpulse)"
if [ ${#missing[@]} -gt 0 ]; then
  echo "missing for screen mirroring: ${missing[*]}"
  echo "  Debian/Ubuntu: sudo apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-pipewire gstreamer1.0-tools"
  echo "  Fedora:        sudo dnf install gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugin-openh264 gstreamer1-plugin-libav pipewire-gstreamer"
  echo "  Arch:          sudo pacman -S gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly gst-libav gst-plugin-pipewire"
  echo "  openSUSE:      sudo zypper install gstreamer-plugins-base gstreamer-plugins-good gstreamer-plugins-bad gstreamer-plugin-openh264 gstreamer-plugins-libav gstreamer-plugin-pipewire"
fi
