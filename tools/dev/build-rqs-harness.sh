#!/bin/sh
# Builds the rquickshare interop peer used by tests/test_quickshare_interop.py.
# Everything lives outside the repo, in ~/multidrop-research (no root needed):
#   rustup (~/.cargo), protoc from the official protobuf release, rquickshare.
set -eu
R="$HOME/multidrop-research"
RQS_REV=378d8ae969941bee4bf60ad34ac9cf8bb7005eb7
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -d "$R/Martichou_rquickshare" ] || git clone https://github.com/Martichou/rquickshare "$R/Martichou_rquickshare"
git -C "$R/Martichou_rquickshare" fetch --depth 1 origin "$RQS_REV" 2>/dev/null || true
git -C "$R/Martichou_rquickshare" checkout -q "$RQS_REV"
mkdir -p "$R/rqs-harness/src"
cp "$HERE/rqs-harness/src/main.rs" "$R/rqs-harness/src/"
sed "s#RQS_CORE_LIB#$R/Martichou_rquickshare/core_lib#" "$HERE/rqs-harness/Cargo.toml.in" > "$R/rqs-harness/Cargo.toml"
# protoc: https://github.com/protocolbuffers/protobuf/releases (v36.2, sha256 checked)
cd "$R/rqs-harness"
PROTOC="$R/tools/protoc/bin/protoc" CARGO_TARGET_DIR="$R/rqs-target" "$HOME/.cargo/bin/cargo" build --release
echo "built $R/rqs-target/release/rqs-harness"
