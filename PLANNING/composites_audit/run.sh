#!/bin/sh
# Reproduce the composites audit findings.
#
#   run.sh catalogue [YYYYMMDD]      A1, A2: how many granules a day returns, and
#                                    what the catalogue can and cannot be asked
#   run.sh accumulate DATA_DIR       A3-A6: cost, memory, coverage, overlap and
#                                    whether quality weighting changes anything
#   run.sh weights DATA_DIR          A6: how much the surviving weights vary
#
# DATA_DIR holds Sentinel-5P NO2 granules from one day. Get them from the URLs
# `catalogue` prints; the audit used eight from 2026-10-09 over a Nordic box.
set -e
here=$(cd "$(dirname "$0")" && pwd)
cmd="${1:?usage: run.sh catalogue|accumulate|weights ...}"; shift
case "$cmd" in
  catalogue) python3 "$here/catalogue_day.py" "${1:-20261009}" --try-cql-bbox ;;
  accumulate)
    d="${1:?data dir}"
    for res in 0.05 0.01; do
      echo "== region at $res deg"
      python3 "$here/accumulate.py" "$d" --res "$res"
    done ;;
  weights) python3 "$here/weights.py" "${1:?data dir}" ;;
  *) echo "unknown command $cmd" >&2; exit 2 ;;
esac
