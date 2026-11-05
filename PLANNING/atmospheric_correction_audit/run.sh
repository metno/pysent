#!/bin/sh
# Reproduce every finding in the atmospheric-correction audit.
#
#   run.sh pairs            A1: matched L1/L2 acquisitions in the NBS archive
#   run.sh probe [n]        A2: the geometry of the first n matched sets
#   run.sh budget water     A3: the reflectance budget on the water frame
#   run.sh budget land      A4: the reflectance budget on the clear-land frame
#   run.sh budget lowsun    A5: the same at a 79.6 degree solar zenith
#                               (the budget modes also apply the aerosol tier
#                                when the day has an aerosol product covering
#                                the frame - Phase 3's acceptance)
#   run.sh tooling          A7: what radiative-transfer tooling the host has
#   run.sh sweep [n]        A9: residual against solar zenith over n triples
#   run.sh spectral         A10: the implied aerosol path across Synergy's bands
#   run.sh aerosol          A6: what the aerosol product holds over the land frame
#   run.sh retrieval        A11: can the loading be retrieved from Level-1 alone?
#   run.sh stretch          Q6: what stretch a corrected picture needs, both platforms
#   run.sh arctic [month]   A16: Arctic OLCI in the catalogue, and matched L1+WFR sets
#   run.sh validate         Phase 6: the shipped correction against ESA, and whether
#                           the published uncertainty covers the error that occurred
#                               (and the per-pixel T865/A865 reference in WFR)
#
# The frames are named here rather than passed in, so a finding is reproduced
# on the same acquisition it was measured on. They are on the mounted archive
# (see the memory file research-data-volume); nothing is downloaded.
#
# **The archive is a rolling window of about seven days.** The Level-1 frame
# the audit first measured the water case on (20261025T111740) had aged out by
# 2026-11-02 while its Level-2 products were still there, which is how a
# missing frame shows up: not as an empty directory but as a half-present
# acquisition. frame() therefore refuses rather than passing empty paths on,
# and a frame that has expired has to be replaced here, with the plan's session
# log saying which number came from which acquisition.
set -e
here=$(cd "$(dirname "$0")" && pwd)
A=/lustre/storeA/NBS2/sentinel/production/NorwAREA/nbsArchive/S3A

# Acquisition start times of the three frames the audit uses.
WATER=20261031T120105      # SZA 79.5, 63 % clear water - the water case
LAND=20261026T072930       # SZA 70.9, 95 % clear land  - the land case
LOWSUN=20261101T063201     # SZA 79.6                   - the high-air-mass case

frame() {  # frame <day> <start> -> three paths, L1 WFR SYN
  day=$1; start=$2
  l1=$(ls "$A/$day/OL-L1-EFR/"*EFR____"$start"_*NT*.zip 2>/dev/null | head -1)
  [ -n "$l1" ] || l1=$(ls "$A/$day/OL-L1-EFR/"*EFR____"$start"_*.zip 2>/dev/null | head -1)
  wfr=$(ls "$A/$day/OL-L2-WFR/"*WFR____"$start"_*.zip 2>/dev/null | head -1)
  syn=$(ls "$A/$day/SY-L2-SYN/"*SYN____"$start"_*.zip 2>/dev/null | head -1)
  for name in l1 wfr syn; do
    eval "value=\$$name"
    [ -n "$value" ] || {
      echo "$A/$day has no $name for $start: the archive is a rolling window and this" >&2
      echo "frame has aged out of it. Pick a current acquisition and repin it above." >&2
      exit 3
    }
  done
  echo "$l1" "$wfr" "$syn"
}

case "$1" in
pairs)
  python3 -I "$here/pairs.py" --platform S3A --months 2026/10 2026/11
  ;;
probe)
  python3 -I "$here/pairs.py" --platform S3A --months 2026/10 --probe "${2:-10}"
  ;;
budget)
  decimate=${3:-4}
  # frame() runs inside a command substitution, which is a subshell, so the
  # "day" it sets there does not reach this shell - name the day here.
  case "${2:-land}" in
    water)  budget_day=2026/10/31; set -- $(frame "$budget_day" "$WATER") ;;
    land)   budget_day=2026/10/26; set -- $(frame "$budget_day" "$LAND") ;;
    lowsun) budget_day=2026/11/01; set -- $(frame "$budget_day" "$LOWSUN") ;;
    *) echo "budget takes water, land or lowsun" >&2; exit 2 ;;
  esac
  # Every aerosol product for the frame's day - budget.py picks the one whose
  # orbit-long window actually covers the frame, and says so if none does.
  aod=$(ls "$A/$budget_day/SY-L2-AOD/"*AOD*.zip 2>/dev/null || true)
  python3 -I "$here/budget.py" --l1 "$1" --wfr "$2" --syn "$3" --decimate "$decimate" \
    ${aod:+--aod $aod} 2>&1 |
    grep -vi "runtimewarning\|nanfunctions\|return np"
  ;;
spectral)
  # Read the decimation before set -- replaces the positional arguments with
  # the three product paths.
  decimate=${2:-8}
  set -- $(frame 2026/10/26 "$LAND")
  python3 -I "$here/spectral.py" --l1 "$1" --syn "$3" --decimate "$decimate" 2>&1 |
    grep -vi "runtimewarning\|nanfunctions\|return np\|invalid value\|divide\|mean of empty\|warning 1"
  ;;
aerosol)
  set -- $(frame 2026/10/26 "$LAND")
  aod=$(ls "$A/2026/10/26/SY-L2-AOD/"*AOD____20261026T072730_*.zip | head -1)
  python3 -I "$here/aerosol.py" --l1 "$1" --aod "$aod" --syn "$3" --decimate 8 2>&1 |
    grep -vi "runtimewarning\|nanfunctions\|return np\|invalid value\|divide\|mean of empty\|warning 1"
  ;;
retrieval)
  # Read the decimation before "set --" replaces the positional arguments with
  # the three product paths. This has bitten twice already.
  decimate=${2:-8}
  # The water frame, because the black-pixel assumption and the WFR aerosol
  # member are both about water.
  retrieval_day=2026/10/31
  set -- $(frame "$retrieval_day" "$WATER")
  aod=$(ls "$A/$retrieval_day/SY-L2-AOD/"*AOD*.zip 2>/dev/null || true)
  python3 -I "$here/retrieval.py" --l1 "$1" --wfr "$2" --syn "$3" \
    --decimate "$decimate" ${aod:+--aod $aod} 2>&1 |
    grep -vi "runtimewarning\|nanfunctions\|return np\|invalid value\|divide\|mean of empty\|warning 1"
  ;;
sweep)
  python3 -I "$here/sweep.py" --count "${2:-28}" --decimate "${3:-8}" 2>&1 |
    grep -vi "runtimewarning\|nanfunctions\|return np\|invalid value\|divide\|mean of empty"
  ;;
validate)
  # Phase 6. Runs the shipped correct_frame, not the audit's re-implementation,
  # and reports coverage: the share of pixels whose real disagreement with ESA
  # fell inside the figure the product published for them. Under Q5 that is the
  # number the product's honesty rests on.
  python3 -I "$here/validate.py" --platform S3A S3B --count "${2:-44}" \
    --decimate "${3:-8}" --arctic "${4:-/mnt/data/arctic-scenes}" 2>&1 |
    grep -vi "runtimewarning\|nanfunctions\|return np\|invalid value\|divide\|mean of empty\|warning 1"
  ;;
arctic)
  # The sea-ice gap of A15. The mounted archive stops at 68.7 N; the catalogue
  # does not, and its records carry THREDDS URLs, so a frame months outside the
  # seven-day window is still fetchable. Default box is Fram Strait to Barents.
  python3 -I "$here/arctic_find.py" --sets --month "${2:-202604}" \
    --bbox "${3:--10,75,35,85}" --min-lat "${4:-78}" --need l1,wfr
  ;;
stretch)
  # Q6: what stretch a corrected true-colour picture needs. Both platforms,
  # because the answer has to be a single default and the two agreeing is the
  # evidence that it can be - S3A gives gamma 0.331, S3B 0.333.
  for platform in S3A S3B; do
    echo "--- $platform"
    python3 -I "$here/stretch.py" --platform "$platform" --months "${2:-2026/11}" \
      --decimate "${3:-8}" --limit "${4:-8}" 2>&1 |
      grep -vi "runtimewarning\|nanfunctions\|return np\|invalid value\|divide\|mean of empty"
  done
  ;;
tooling)
  python3 - <<'PY'
import importlib
for name in ("Py6S", "smac", "scipy", "netCDF4", "numpy", "osgeo"):
    try:
        module = importlib.import_module(name)
        print(f"{name:10s} present  {getattr(module, '__version__', '')}")
    except ImportError:
        print(f"{name:10s} absent")
PY
  ;;
*) sed -n '2,12p' "$0"; exit 2 ;;
esac
