"""What it costs to composite N granules onto one fixed grid.

The claim in B.9 is that memory stays O(grid): two accumulator grids plus one
granule at a time, so cost in memory does not grow with the number of inputs.
This measures that, and the things that follow from it - how much of a region a
day actually fills, how many granules land on a typical cell, and whether
weighting the mean by the quality value changes the answer.

    python3 accumulate.py DATA_DIR [--res 0.05] [--max 8]
"""
from __future__ import annotations

import argparse
import pathlib
import resource
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from pysent._swath import (  # noqa: E402
    open_root,
    read_array,
    read_raw_array,
    warp_swath,
)

NO2 = "/PRODUCT/nitrogendioxide_tropospheric_column"
QA = "/PRODUCT/qa_value"
# Mainland Norway plus shelf, snapped to whole cells by the grid step.
REGION = (2.0, 57.0, 32.0, 72.0)


def peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def grid_shape(region, res):
    width = int(round((region[2] - region[0]) / res))
    height = int(round((region[3] - region[1]) / res))
    return height, width


def one_granule(path, region, res, scratch, qa_min=0.5):
    """One granule warped onto the fixed region grid, plus its weight grid."""
    root = open_root(str(path))
    lat = read_array(root, "/PRODUCT/latitude")
    lon = read_array(root, "/PRODUCT/longitude")
    values = read_array(root, NO2)
    qa_raw = read_raw_array(root, QA)
    threshold = int(round(qa_min * 100))
    keep = (qa_raw > threshold) & np.isfinite(values)
    if not keep.any():
        return None, None
    masked = np.where(keep, values, np.nan).astype(np.float32)
    # The quality value doubles as the weight: it is already 0..100.
    weights = np.where(keep, qa_raw.astype(np.float32) / 100.0, np.nan).astype(np.float32)

    out_value = str(scratch / "value.tif")
    out_weight = str(scratch / "weight.tif")
    try:
        warp_swath(values=masked, lat=lat, lon=lon, output_path=out_value, epsg=4326,
                   resolution=res, bounds=region, resampling="average", scratch=scratch)
        warp_swath(values=weights, lat=lat, lon=lon, output_path=out_weight, epsg=4326,
                   resolution=res, bounds=region, resampling="average", scratch=scratch)
    except Exception as exc:
        print(f"    warp failed: {str(exc)[:70]}")
        return None, None
    a = gdal.Open(out_value); value = a.GetRasterBand(1).ReadAsArray(); a = None
    b = gdal.Open(out_weight); weight = b.GetRasterBand(1).ReadAsArray(); b = None
    return value, weight


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=pathlib.Path)
    parser.add_argument("--res", type=float, default=0.05)
    parser.add_argument("--max", type=int, default=8)
    parser.add_argument("--qa-min", type=float, default=0.5)
    args = parser.parse_args()

    granules = sorted(args.data_dir.glob("S5P_*NO2*.nc"))[: args.max]
    if not granules:
        print("no granules found", file=sys.stderr)
        return 1

    height, width = grid_shape(REGION, args.res)
    print(f"region {REGION} at {args.res} deg = {width} x {height} = {width*height/1e6:.2f} Mcell")
    print(f"{len(granules)} granules, qa_min {args.qa_min}\n")

    total = np.zeros((height, width), dtype=np.float64)
    weight_total = np.zeros((height, width), dtype=np.float64)
    plain_total = np.zeros((height, width), dtype=np.float64)
    count = np.zeros((height, width), dtype=np.uint16)

    scratch = pathlib.Path("/tmp/composite_scratch")
    scratch.mkdir(exist_ok=True)
    print(f"{'n':>2} {'granule':22s} {'own cells':>10s} {'filled %':>9s} "
          f"{'mean cells/px':>13s} {'time':>7s} {'peak MB':>8s}")
    used, skipped = [], []
    started = time.time()
    for index, path in enumerate(granules, 1):
        step = time.time()
        value, w = one_granule(path, REGION, args.res, scratch, args.qa_min)
        if value is None:
            skipped.append(path.name)
            print(f"{index:>2} {path.name[22:44]:22s} {'-':>10s}   skipped (nothing above qa_min)")
            continue
        present = np.isfinite(value) & np.isfinite(w)
        total[present] += value[present] * w[present]
        weight_total[present] += w[present]
        plain_total[present] += value[present]
        count[present] += 1
        used.append(path.name)
        filled = 100 * np.count_nonzero(count) / count.size
        overlap = count[count > 0].mean()
        print(f"{index:>2} {path.name[22:44]:22s} {int(present.sum()):>10,} {filled:8.2f}% "
              f"{overlap:13.2f} {time.time()-step:6.1f}s {peak_mb():8.0f}")

    composite = np.where(weight_total > 0, total / np.maximum(weight_total, 1e-12), np.nan)
    plain = np.where(count > 0, plain_total / np.maximum(count, 1), np.nan)
    print(f"\ntotal {time.time()-started:.1f}s, peak {peak_mb():.0f} MB")
    print(f"used {len(used)} granules, skipped {len(skipped)}")
    filled = np.count_nonzero(count)
    print(f"filled {filled:,} of {count.size:,} cells = {100*filled/count.size:.2f}% of the region")
    if filled:
        print(f"granules per filled cell: mean {count[count>0].mean():.2f} "
              f"max {count.max()}  (cells seen once: {100*np.mean(count[count>0]==1):.1f}%)")
        good = np.isfinite(composite)
        print(f"composite p01/p50/p99: "
              f"{np.nanpercentile(composite[good],[1,50,99]).round(0).tolist()} (x1e-6 below)")
        print(f"   in mol m-2: {[f'{v:.3g}' for v in np.nanpercentile(composite[good],[1,50,99])]}")

        # Does weighting by the quality value change the answer enough to be
        # worth the second accumulator and the second warp per granule?
        both = good & np.isfinite(plain)
        multi = both & (count > 1)
        spread = np.nanpercentile(composite[good], 99) - np.nanpercentile(composite[good], 1)
        for label, sel in (("all filled cells", both), ("cells with >1 granule", multi)):
            if sel.sum() < 50:
                print(f"   {label}: too few"); continue
            d = np.abs(composite[sel] - plain[sel])
            print(f"   qa-weighted vs plain mean, {label}: median |diff| {np.median(d):.3g}, "
                  f"p99 {np.percentile(d,99):.3g} = {100*np.percentile(d,99)/spread:.2f}% of the p1-p99 spread")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
