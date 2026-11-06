"""What the aerosol product says, and whether ESA's two products agree at 560 nm.

Phase 2 left the 560 nm band 0.0126 low against Synergy surface reflectance,
negative in 12 of 13 frames and independent of solar zenith. Before adding an
aerosol term that moves all three bands at once, this asks two questions of the
archive:

1. **What does the aerosol product actually hold** over the frame - optical
   depth, Angstrom exponent, single-scattering albedo - so Phase 3 knows what
   it is interpolating rather than guessing a model.
2. **Do ESA's own two products agree with each other?** ``SY_2_AOD`` carries
   ``Surface_reflectance_440/550/670`` on its coarse grid, retrieved by a
   different processor from ``SY_2_SYN``'s ``SDR_Oa**``. If the two disagree at
   550/560 by something like the size of our residual, the bias is in the
   reference rather than in the correction, and no amount of aerosol modelling
   will close it.

The comparison is over the Level-1 frame's own bounding box, by median, because
the aerosol grid is 4026 x 324 against the image's millions of pixels and this
is a question about levels, not about texture.

    python3 aerosol.py --l1 L1.zip --aod AOD.zip [--syn SYN.zip] [--decimate 8]

An orbit-long aerosol product covers several Level-1 frames, so the script
checks that the frame's sensing time falls inside the aerosol product's and
refuses otherwise - which is the check Phase 3's explicit ``aerosol`` option
will need.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

# The aerosol readers used to be local copies here. Phase 3 needs the same
# ones in the budget script to apply the tier, and two copies of a reader are
# two things to keep in step, so they live there now and this calls them.
from budget import (  # noqa: E402
    BANDS,
    NEAREST_AEROSOL_NM,
    aod_field,
    covers,
    scratch_dir,
    sensing_window,
    tie_field,
)
from sweep import corrected_frame  # noqa: E402

from pysent._sen3 import sen3_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--l1", required=True)
    parser.add_argument("--aod", required=True)
    parser.add_argument("--syn")
    parser.add_argument("--decimate", type=int, default=8)
    args = parser.parse_args()

    frame_window = sensing_window(args.l1)
    aod_window = sensing_window(args.aod)
    print(f"frame   {frame_window[0]} to {frame_window[1]}")
    print(f"aerosol {aod_window[0]} to {aod_window[1]}")
    if not covers(aod_window, frame_window):
        print("\nthe aerosol product does not cover the frame - this is the check Phase 3 needs")
        return 2
    print("the aerosol product covers the frame\n")

    scratch = scratch_dir("ac-aerosol-")
    frame = corrected_frame(args.l1, args.decimate)
    shape = frame["shape"]
    root = sen3_root(args.l1)
    latitude = tie_field(root, "tie_geo_coordinates.nc", "latitude", shape, scratch)
    longitude = tie_field(root, "tie_geo_coordinates.nc", "longitude", shape, scratch)
    land = frame["land"]
    box = (
        float(np.nanmin(latitude)), float(np.nanmax(latitude)),
        float(np.nanmin(longitude)), float(np.nanmax(longitude)),
    )
    print(f"frame covers {box[0]:.2f} to {box[1]:.2f} N, {box[2]:.2f} to {box[3]:.2f} E; "
          f"{int(land.sum()):,} clear land pixels\n")

    aod_root = sen3_root(args.aod)
    aod_lat = aod_field(aod_root, "latitude", scratch)
    aod_lon = aod_field(aod_root, "longitude", scratch)
    inside = (
        (aod_lat >= box[0]) & (aod_lat <= box[1]) & (aod_lon >= box[2]) & (aod_lon <= box[3])
    )
    print(f"aerosol grid {aod_lat.shape[1]} x {aod_lat.shape[0]}, "
          f"{int(inside.sum()):,} cells inside the frame\n")
    if not inside.any():
        print("no aerosol cell falls inside the frame")
        return 2

    print("what the aerosol product holds there (p05 / p50 / p95 over those cells):")
    for variable in (
        "AOD_550", "AOD_550_uncertainty", "AOD_440", "AOD_670",
        "ANG550_865", "SSA_550", "FM_AOD550", "cloud_fraction_nadir",
    ):
        try:
            values = aod_field(aod_root, variable, scratch)
        except (ValueError, RuntimeError):
            print(f"  {variable:22s} absent")
            continue
        picked = values[inside]
        picked = picked[np.isfinite(picked)]
        if picked.size == 0:
            print(f"  {variable:22s} all missing inside the frame")
            continue
        low, mid, high = np.percentile(picked, [5, 50, 95])
        print(f"  {variable:22s} {low:9.4f} {mid:9.4f} {high:9.4f}   "
              f"({100.0 * picked.size / max(1, int(inside.sum())):.0f} % present)")

    print("\nthree surface reflectances over the same ground, by median:")
    print(f"{'band':12s} {'ours':>9s} {'SYN SDR':>9s} {'AOD prod':>9s} "
          f"{'ours-SYN':>9s} {'SYN-AOD':>9s}")
    syn_root = sen3_root(args.syn) if args.syn else None
    for band in BANDS:
        nm = NEAREST_AEROSOL_NM[band]
        ours = float(np.nanmedian(np.where(land, frame["surfaces"][band], np.nan)))

        synergy = float("nan")
        if syn_root is not None:
            from budget import level2_reflectance

            try:
                values = level2_reflectance(
                    syn_root, f"Syn_Oa{band:02d}_reflectance.nc", f"SDR_Oa{band:02d}",
                    shape, scratch,
                )
                # Both medians over the *same* pixels. Taking each over its own
                # valid pixels made "ours - SYN" a difference of two
                # differently-sampled populations, which at 490 nm read 0.0101
                # where the like-for-like figure is 0.0071.
                keep = land & np.isfinite(frame["surfaces"][band]) & np.isfinite(values)
                ours = float(np.nanmedian(np.where(keep, frame["surfaces"][band], np.nan)))
                synergy = float(np.nanmedian(np.where(keep, values, np.nan)))
            except (ValueError, RuntimeError):
                pass

        try:
            product = aod_field(aod_root, f"Surface_reflectance_{nm}", scratch)
            picked = product[inside]
            picked = picked[np.isfinite(picked)]
            esa = float(np.median(picked)) if picked.size else float("nan")
        except (ValueError, RuntimeError):
            esa = float("nan")

        print(f"Oa{band:02d}/{nm:3d}nm {ours:9.4f} {synergy:9.4f} {esa:9.4f} "
              f"{ours - synergy:9.4f} {synergy - esa:9.4f}")

    print("\nIf ours-SYN and SYN-AOD are similar in size at 560/550, the disagreement is "
          "\nbetween ESA's own two retrievals and not a defect in this correction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
