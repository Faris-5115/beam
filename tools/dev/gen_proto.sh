#!/bin/sh
# Regenerate the Quick Share protobuf modules from the .proto files that
# NearDrop (Unlicense) collected from Chromium. Needs grpcio-tools in the venv.
set -eu
cd "$(dirname "$0")/../.."
Q=src/multidrop/backends/quickshare
.venv/bin/python -m grpc_tools.protoc -I "$Q/proto" --python_out="$Q/pb" --pyi_out="$Q/pb" "$Q"/proto/*.proto
# protoc emits top-level imports ("import securemessage_pb2"); make them package-relative.
sed -i -E 's/^import ([a-z_]+_pb2) as/from . import \1 as/' "$Q"/pb/*_pb2.py
touch "$Q/pb/__init__.py"
