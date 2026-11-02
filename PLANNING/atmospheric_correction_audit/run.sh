#!/bin/sh
# Reproduce every finding in the atmospheric-correction audit.
#
#   run.sh pairs            A1: matched L1/L2 acquisitions in the NBS archive
#   run.sh probe [n]        A2: the geometry of the first n matched sets
#   run.sh budget water     A3: the reflectance budget on the water frame
#   run.sh budget land      A4: the reflectance budget on the clear-land frame
#   run.sh budget lowsun    A5: the same at a 79.6 degree solar zenith
#   run.sh tooling          A7: what radiative-transfer tooling the host has
#   run.sh sweep [n]        A9: residual against solar zenith over n triples
#   run.sh spectral         A10: the implied aerosol path across Synergy's bands
#   run.sh aerosol          A6: what the aerosol product holds over the land frame
#
# The frames are named here rather than passed in, so a finding is reproduced
# on the same acquisition it was measured on. They are on the mounted archive
# (see the memory file research-data-volume); nothing is downloaded.
set -e
here=$(cd "$(dirname "$0")" && pwd)
A=/lustre/storeA/NBS2/sentinel/production/NorwAREA/nbsArchive/S3A

# Acquisition start times of the three frames the audit uses.
WATER=20261025T111740      # SZA 70.6, 48 % clear water - the water case
LAND=20261026T072930       # SZA 70.9, 95 % clear land  - the land case
LOWSUN=20261101T063201     # SZA 79.6                   - the high-air-mass case

frame() {  # frame <day> <start> -> three paths, L1 WFR SYN
  day=$1; start=$2
  l1=$(ls "$A/$day/OL-L1-EFR/"*EFR____"$start"_*NT*.zip 2>/dev/null | head -1)
  [ -n "$l1" ] || l1=$(ls "$A/$day/OL-L1-EFR/"*EFR____"$start"_*.zip | head -1)
  wfr=$(ls "$A/$day/OL-L2-WFR/"*WFR____"$start"_*.zip | head -1)
  syn=$(ls "$A/$day/SY-L2-SYN/"*SYN____"$start"_*.zip | head -1)
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
  case "${2:-land}" in
    water)  set -- $(frame 2026/10/25 "$WATER") ;;
    land)   set -- $(frame 2026/10/26 "$LAND") ;;
    lowsun) set -- $(frame 2026/11/01 "$LOWSUN") ;;
    *) echo "budget takes water, land or lowsun" >&2; exit 2 ;;
  esac
  python3 -I "$here/budget.py" --l1 "$1" --wfr "$2" --syn "$3" --decimate "$decimate" 2>&1 |
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
sweep)
  python3 -I "$here/sweep.py" --count "${2:-28}" --decimate "${3:-8}" 2>&1 |
    grep -vi "runtimewarning\|nanfunctions\|return np\|invalid value\|divide\|mean of empty"
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
