#!/usr/bin/env bash
# Add magnet sockets to every STL file in a directory.
#
# Output goes to the same path with its `input` component replaced by `output`,
# e.g. assets/input/wood-elf-bases/25x25 -> assets/output/wood-elf-bases/25x25,
# and each file gets a `_with_<D>x<H>_magnet` suffix.
set -euo pipefail

SCRIPT="$(dirname "${BASH_SOURCE[0]}")/blender/subtract-magnet-hole.py"

usage() {
    cat <<EOF
Usage: $(basename "$0") --in DIR [--diameter MM] [--height MM]

  -i, --in DIR         Directory of input STL files (must contain an 'input' path component)
  -d, --diameter MM    Magnet diameter in mm (default: 5)
  -H, --height MM      Magnet height in mm (default: 2)
  -h, --help           Show this help
EOF
}

# Format a number for the filename suffix: 5 -> 5, 5.0 -> 5, 5.50 -> 5.5
fmt() {
    local n="$1"
    if [[ "$n" == *.* ]]; then
        n="${n%%+(0)}"
        n="${n%.}"
    fi
    echo "$n"
}

shopt -s extglob nullglob nocaseglob

in_dir=""
diameter="5"
height="2"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -i | --in) in_dir="${2:?--in needs a value}"; shift 2 ;;
        -d | --diameter) diameter="${2:?--diameter needs a value}"; shift 2 ;;
        -H | --height) height="${2:?--height needs a value}"; shift 2 ;;
        -h | --help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [[ -z "$in_dir" ]]; then
    echo "Missing --in DIR" >&2
    usage >&2
    exit 2
fi

for value in "$diameter" "$height"; do
    if ! [[ "$value" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
        echo "Not a positive number: $value" >&2
        exit 2
    fi
done

in_dir="${in_dir%/}"
if [[ ! -d "$in_dir" ]]; then
    echo "Not a directory: $in_dir" >&2
    exit 1
fi

# Replace the first path component named 'input' with 'output'
case "/$in_dir/" in
    */input/*) out_dir="$(sed 's#\(^\|/\)input\(/\|$\)#\1output\2#' <<<"$in_dir")" ;;
    *) echo "Input directory must contain an 'input' path component: $in_dir" >&2; exit 2 ;;
esac

suffix="_with_$(fmt "$diameter")x$(fmt "$height")_magnet"
files=("$in_dir"/*.stl)

if [[ ${#files[@]} -eq 0 ]]; then
    echo "No STL files found in $in_dir" >&2
    exit 1
fi

mkdir -p "$out_dir"

for f in "${files[@]}"; do
    filename="$(basename "$f")"
    out="$out_dir/${filename%.*}${suffix}.${filename##*.}"
    blender -b --python-exit-code 1 -P "$SCRIPT" -- \
        --in "$f" --out "$out" --diameter "$diameter" --height "$height"
done

echo "Processed ${#files[@]} file(s) into $out_dir"
