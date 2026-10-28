#!/bin/bash
# Build the original, pinned omdrop-owl .deb without installing packages or
# touching Wi-Fi. Debian/Ubuntu; system libpcap/libnl runtimes must be present.
# Usage: tools/build-omdrop-owl.sh [WORKDIR]
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=${1:-$HOME/multidrop-research/omdrop-package}
REV=a063387fa8489b206f7d5dd8ed82b79825b1111f
SRC=$WORK/omdrop-owl
MULTIARCH=$(dpkg-architecture -qDEB_HOST_MULTIARCH)
LIB=/usr/lib/$MULTIARCH
SYS=$WORK/sysroot/usr
mkdir -p "$WORK/debs" "$ROOT/build/airdrop"

if [ ! -d "$SRC/.git" ]; then
  git clone -q https://github.com/t4t5/omdrop-owl.git "$SRC"
fi
# Do not discard edits in an existing checkout.
if ! git -C "$SRC" diff --quiet || ! git -C "$SRC" diff --cached --quiet; then
  echo "Local edits in $SRC; use another WORKDIR" >&2
  exit 1
fi
git -C "$SRC" -c advice.detachedHead=false checkout -q "$REV"
# shellcheck source=/dev/null
. "$SRC/pins"

cd "$WORK/debs"
for pkg in libev-dev libpcap0.8-dev libpcap-dev libnl-3-dev libnl-genl-3-dev libnl-route-3-dev; do
  compgen -G "${pkg}_*.deb" >/dev/null || apt-get download "$pkg"
done
for deb in ./*.deb; do dpkg -x "$deb" "$WORK/sysroot"; done

OWL=$SRC/upstream/owl
if [ ! -d "$OWL/.git" ]; then
  mkdir -p "$SRC/upstream"
  git clone -q https://github.com/jedbillyb/owl.git "$OWL"
  git -C "$OWL" -c advice.detachedHead=false checkout -q "$OWL_COMMIT"
  git -C "$OWL" submodule update -q --init --recursive
fi
# Prime CMake's paths only. The original build-deb.sh still applies all
# upstream patches, builds, stages the unmodified tools and packages them.
# Static libev avoids a new system runtime dependency. Use installed shared
# pcap/nl libraries rather than the extracted dev packages' dangling symlinks.
export CPATH="$SYS/include${CPATH:+:$CPATH}"
export CMAKE_PREFIX_PATH="$SYS${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"
cmake -S "$OWL" -B "$OWL/build" -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
  -Dev_INCLUDE="$SYS/include" -Dev_LIBRARY="$SYS/lib/$MULTIARCH/libev.a" \
  -Dpcap_INCLUDE="$SYS/include" -Dpcap_LIBRARY="$LIB/libpcap.so.0.8" \
  -Dnl_INCLUDE="$SYS/include/libnl3" -Dnl_LIBRARY="$LIB/libnl-3.so.200" \
  -Dnlgenl_LIBRARY="$LIB/libnl-genl-3.so.200" \
  -Dnlroute_LIBRARY="$LIB/libnl-route-3.so.200" >/dev/null
"$SRC/tools/build-deb.sh"
cp "$SRC"/omdrop-owl_*.deb "$ROOT/build/airdrop/"
printf 'omdrop-owl %s\nOWL %s\nomdrop-awdl %s\nlibev linked statically; built without root\n' \
  "$REV" "$OWL_COMMIT" "$OMDROP_AWDL_COMMIT" > "$ROOT/build/airdrop/build-info.txt"
cd "$ROOT/build/airdrop"
sha256sum ./*.deb > SHA256SUMS
echo "Package ready in $ROOT/build/airdrop (not installed)."
