#!/bin/sh
# Reproduce the review-study findings and the checks each fix is held to.
#
#   run.sh flux PRODUCT.zip        R1: the band-indexed tables arrive bottom-up
#   run.sh numbers PRODUCT.zip     R1: the physical numbers, before and after
#   run.sh freeze                  the Sentinel-5P freeze: branch vs main pixels
#
# PRODUCT.zip is an OLCI Level-1 EFR product; the NBS archive and the data
# volume both hold them (see the memory file research-data-volume).
set -e
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)

case "$1" in
flux)     python3 "$here/flux_orientation.py" "$2" ;;
numbers)  python3 "$here/truecolor_numbers.py" "$2" ${3:+--decimate "$3"} ;;
freeze)
  work=${2:-/tmp/pysent-freeze}
  rm -rf "$work" && mkdir -p "$work/main"
  git -C "$repo" archive origin/main | tar -x -C "$work/main"
  python3 "$here/s5p_freeze_check.py" --src "$repo/src" --out "$work/branch"
  python3 "$here/s5p_freeze_check.py" --src "$work/main/src" --out "$work/main-out" \
          --fixtures "$work/main/tests/data"
  diff "$work/main-out/hashes.txt" "$work/branch/hashes.txt" \
    && echo "S5P freeze holds: zero differing pixel hashes"
  ;;
*) sed -n '2,12p' "$0"; exit 2 ;;
esac
