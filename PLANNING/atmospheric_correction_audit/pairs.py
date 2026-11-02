"""Which OLCI acquisitions can an atmospheric correction be validated on?

A correction needs a Level-1 frame and, for the same acquisition, a corrected
Level-2 product to be judged against. This finds the matched sets in the
mounted NBS archive and reports what each one is worth: the solar zenith angle
(the first thing that decides whether a correction is even defined), and how
much of the frame is clear water and clear land.

Matching is by the sensing start and stop times and the orbit/frame fields in
the product name, which are identical across the three product types for one
acquisition.

    python3 pairs.py [--platform S3A] [--months 2026/10 2026/11] [--probe 6]

``--probe`` opens that many of the matched sets and reads their geometry, which
costs a few seconds each; without it the listing is name-only.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from budget import scratch_dir  # noqa: E402

from pysent._olci_l1 import quality_mask  # noqa: E402
from pysent._sen3 import (  # noqa: E402
    extract_member,
    find_member,
    open_sen3_variable,
    read_packed,
    sen3_root,
)

ARCHIVE = pathlib.Path("/lustre/storeA/NBS2/sentinel/production/NorwAREA/nbsArchive")
TYPES = {"l1": "OL-L1-EFR", "wfr": "OL-L2-WFR", "syn": "SY-L2-SYN", "aod": "SY-L2-AOD"}
# S3A_OL_1_EFR____<start>_<stop>_<created>_<duration>_<cycle>_<orbit>_<frame>_...
KEY = re.compile(r"^S3[AB]_\w{2}_\w_\w{3}____(\d{8}T\d{6})_(\d{8}T\d{6})_\d{8}T\d{6}_(\d{4})_(\d{3})_(\d{3})")


def acquisition_key(name):
    match = KEY.match(name)
    return (match.group(1), match.group(2), match.group(4), match.group(5)) if match else None


def collect(platform, months):
    """{acquisition key: {kind: path}} for every product in those months."""
    found = collections.defaultdict(dict)
    for month in months:
        for kind, directory in TYPES.items():
            for day in sorted((ARCHIVE / platform / month).glob("*")):
                for product in sorted((day / directory).glob("*.zip")):
                    key = acquisition_key(product.name)
                    if key:
                        # Prefer the non-time-critical reprocessing when both exist.
                        existing = found[key].get(kind)
                        if existing is None or ("_NT_" in product.name and "_NR_" in existing.name):
                            found[key][kind] = product
    return found


def probe(l1_path):
    """Solar zenith and the clear land/water split of one Level-1 frame."""
    scratch = scratch_dir("ac-pairs-")
    root = sen3_root(str(l1_path))
    dataset = open_sen3_variable(
        extract_member(root, find_member(root, "tie_geometries.nc"), scratch), "SZA"
    )
    band = dataset.GetRasterBand(1)
    # One pixel in sixteen is plenty for a median angle and a flag share.
    sza = read_packed(band, decimate=16, log10=False)
    dataset = None

    dataset = open_sen3_variable(
        extract_member(root, find_member(root, "Oa08_radiance.nc"), scratch), "Oa08_radiance"
    )
    columns, rows = dataset.RasterXSize, dataset.RasterYSize
    dataset = None
    shape = (max(1, rows // 16), max(1, columns // 16))

    rejected, _, bright = quality_mask(root, scratch, shape, ["invalid", "saturated"])
    land, _, _ = quality_mask(root, scratch, shape, ["land"])
    total = land.size
    usable = ~rejected & ~bright
    return {
        "sza": float(np.nanmedian(sza)),
        "sza_min": float(np.nanmin(sza)),
        "water": 100.0 * float((usable & ~land).sum()) / total,
        "land": 100.0 * float((usable & land).sum()) / total,
        "bright": 100.0 * float(bright.sum()) / total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="S3A")
    parser.add_argument("--months", nargs="+", default=["2026/10", "2026/11"])
    parser.add_argument("--probe", type=int, default=0)
    args = parser.parse_args()

    found = collect(args.platform, args.months)
    complete = {k: v for k, v in found.items() if "l1" in v and ("wfr" in v or "syn" in v)}
    print(f"{args.platform}, months {' '.join(args.months)}: {len(found)} acquisitions, "
          f"{len(complete)} with a Level-1 and at least one corrected reference")
    counts = collections.Counter(tuple(sorted(v)) for v in found.values())
    for kinds, n in counts.most_common():
        print(f"   {n:4d} x {'+'.join(kinds)}")

    if not args.probe:
        return 0

    print(f"\nProbing {min(args.probe, len(complete))} of them "
          f"(SZA from the tie grid, shares of the frame from the Level-1 flags):\n")
    print(f"{'acquisition':22s} {'refs':10s} {'SZA med':>8s} {'SZA min':>8s} "
          f"{'clear water':>12s} {'clear land':>11s} {'bright':>8s}")
    for key in sorted(complete)[: args.probe]:
        kinds = "+".join(sorted(k for k in complete[key] if k != "l1"))
        try:
            stats = probe(complete[key]["l1"])
        except Exception as error:  # a truncated archive copy should not stop the survey
            print(f"{key[0]:22s} {kinds:10s} unreadable: {error}")
            continue
        print(f"{key[0]:22s} {kinds:10s} {stats['sza']:8.1f} {stats['sza_min']:8.1f} "
              f"{stats['water']:11.1f}% {stats['land']:10.1f}% {stats['bright']:7.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
