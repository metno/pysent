#!/usr/bin/env bash
# Re-runs the findings of PLANNING/TODO_PLANNING_s5p_s3_pipelines.md.
#
#   run.sh survey   OUT.jsonl            catalogue counts, bbox/reference statistics (needs network)
#   run.sh detect   SAMPLE.jsonl         old vs anchored platform detection (needs network for S1/S2)
#   run.sh fetch    DATA_DIR             download the named S5P test granules (~800 MB, needs network)
#   run.sh probe    DATA_DIR             footprint / antimeridian / QA statistics per granule
#   run.sh regrid   DATA_DIR OUT_DIR     geoloc vs polygon regridding: geometry, values, time, peak RSS
#   run.sh variables DATA_DIR            variable names, units and swath widths of the other S5P types
#
# Only GDAL (python3-gdal, 3.8.4 in CI) and numpy are used. Run the timing steps on an idle machine and
# alternate variants (the first read of a file warms the page cache).
#
# The Sentinel-3 steps are the s3_*.py scripts in this directory (each has a usage line in its docstring),
# in this order; the product titles to feed them are in the plan, section 7:
#   s3_find.py / s3_pick.py     choose real, non-empty products of a type (HEAD content-length; flags 0-byte stubs)
#   s3_stubs.py                 stub rate by timeliness (NR / ST / NT) and month
#   s3_zipinfo.py URL.zip       members, STORED vs DEFLATED, bytes fetched: range reads over /vsizip//vsicurl/
#   inspect_nc.py MEMBER.nc     variable names, units, packing (works on /vsizip//vsicurl/ paths)
#   s3_flags.py FILE VAR        every flag name/mask, optionally its frequency
#   s3_positional_error.py      tie-point and decimated-geolocation error in metres vs the full lat/lon arrays
#   s3_warp.py / s3_bench_warp.py   geolocation-array warp variants, one process each: seconds and peak RSS
#   s3_render_{wfr,lst,vg1,olci_l1}.py   the pictures in the owner-decision page (colour ranges are proposals)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here"
cmd="${1:?usage: run.sh survey|detect|fetch|probe|regrid|variables ...}"; shift

# The named test granules (catalogue identifier -> file); see the plan, section 7.
S5P_NO2_DAY=(
  "2026/10/09/TR-L2-NO2/S5P_NRTI_L2__NO2____20261009T111407_20261009T111907_46578_03_020901_20261009T115729.nc"
  "2026/10/06/TR-L2-NO2/S5P_NRTI_L2__NO2____20261006T153407_20261006T153907_46538_03_020901_20261006T160505.nc"
)
S5P_NO2_NIGHT=(
  "2026/09/30/TR-L2-NO2/S5P_NRTI_L2__NO2____20260930T073407_20260930T073907_46448_03_020901_20260930T091103.nc"
  "2026/10/17/TR-L2-NO2/S5P_NRTI_L2__NO2____20261017T172254_20261017T172754_46695_03_020901_20261017T185951.nc"
  "2026/10/10/TR-L2-NO2/S5P_NRTI_L2__NO2____20261010T074407_20261010T074907_46590_03_020901_20261010T092428.nc"
)
S5P_NO2_OFFL=(
  "2026/07/02/TR-L2-NO2/S5P_OFFL_L2__NO2____20260702T161739_20260702T175909_45177_03_020901_20260704T082228.nc"
)
BASE="https://nbstds.met.no/thredds/fileServer/nbsArchive/S5P"

case "$cmd" in
  survey)  python3 catalogue_survey.py --out "${1:?out.jsonl}" --per-type 30 ;;
  detect)  python3 detect_audit.py "${1:?sample.jsonl}" --fresh ;;
  fetch)
    d="${1:?data dir}"; mkdir -p "$d"
    for f in "${S5P_NO2_DAY[@]}" "${S5P_NO2_NIGHT[@]}" "${S5P_NO2_OFFL[@]}"; do
      [ -s "$d/$(basename "$f")" ] || curl -sf -o "$d/$(basename "$f")" "$BASE/$f"
    done; ls -la "$d" ;;
  probe)
    for f in "${1:?data dir}"/*.nc; do echo "=== $f"; python3 -W ignore s5p_probe.py "$f"; done ;;
  regrid)
    d="${1:?data dir}"; o="${2:?out dir}"
    day="$d/$(basename "${S5P_NO2_DAY[0]}")"; off="$d/$(basename "${S5P_NO2_OFFL[0]}")"
    pole="$d/$(basename "${S5P_NO2_NIGHT[2]}")"
    echo "## geometry of each method on a mid-latitude granule (coverage raster, no QA mask)"
    for res in 0.1 0.05 0.025; do
      for m in polygons geoloc-near geoloc-avg; do python3 -W ignore s5p_regrid.py "$day" $m --epsg 4326 --res $res --coverage --out "$o/res$res"; done
      python3 s5p_compare.py "$o/res$res"
    done
    echo "## values, QA 0.5 and 0.75"
    for q in 0.5 0.75; do for m in polygons-ss3 geoloc-near geoloc-avg; do python3 -W ignore s5p_regrid.py "$day" $m --epsg 4326 --res 0.05 --qa-min $q --out "$o/val"; done; done
    for t in qa50 qa75; do python3 s5p_valdiff.py "$o/val" $t; done
    echo "## pole-crossing granule: EPSG:4326 versus EPSG:32661"
    for m in polygons geoloc-near; do python3 -W ignore s5p_regrid.py "$pole" $m --epsg 4326 --res 0.05 --coverage --out "$o/polar4326"; done
    python3 s5p_compare.py "$o/polar4326"
    for m in polygons geoloc-near geoloc-avg; do python3 -W ignore s5p_regrid.py "$pole" $m --epsg 32661 --res 5000 --coverage --out "$o/polar32661"; done
    python3 s5p_compare.py "$o/polar32661"
    echo "## a full OFFL orbit: time and peak RSS (each variant in its own process; alternate when repeating)"
    for res in 0.1 0.05; do for m in geoloc-near geoloc-avg; do python3 -W ignore s5p_regrid.py "$off" $m --epsg 4326 --res $res --qa-min 0.5 --out "$o/offl$res"; done; done
    python3 -W ignore s5p_regrid.py "$off" polygons --epsg 4326 --res 0.1 --qa-min 0.5 --out "$o/offl0.1" ;;
  variables) python3 -W ignore s5p_variables.py "${1:?data dir}"/*.nc ;;
  *) echo "unknown command $cmd" >&2; exit 2 ;;
esac
