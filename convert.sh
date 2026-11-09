#!/usr/bin/env bash
set -euo pipefail

mkdir -p magnet_bases

for f in bases/*.stl; do
    [ -e "$f" ] || continue
    filename="$(basename "$f")"
    blender -b -P blender/subtract-magnet-hole.py -- --in "$f" --out "magnet_bases/$filename" -d 5.0 -H 2.0
done
