"""R7: how much of an SLSTR product is lost to the nearest-neighbour warp?

``warp_swath`` forces ``resampling="near"`` whenever a categorical companion
band rides along with the values, because averaging two reject reasons gives a
third reason that means nothing. On the 1 km SLSTR grid that leaves output
cells with no source pixel at all - the dashed gaps a reviewer sees.

This measures what a second warp would recover: the same frame warped once with
the companion (so, ``near``) and once with the values alone and ``average``,
and the share of cells the averaged warp fills that the nearest one does not.

    python3 slstr_gaps.py PRODUCT.zip [PRODUCT.zip ...] [--product lst]
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from pysent._sen3 import (  # noqa: E402
    extract_member,
    find_member,
    open_sen3_variable,
    read_packed,
    sen3_root,
)
from pysent._swath import choose_grid, grid_bounds, warp_swath  # noqa: E402
from pysent.s3 import S3_PRODUCTS, SLSTR_GROUND_SAMPLING_M  # noqa: E402


def read(root, member, variable, scratch, decimate=1):
    local = extract_member(root, find_member(root, member), scratch)
    dataset = open_sen3_variable(local, variable)
    values = read_packed(dataset.GetRasterBand(1), decimate=decimate, log10=False)
    dataset = None
    return values


def warped(values, lat, lon, scratch, epsg, resolution, bounds, *, resampling, companion):
    path = str(scratch / f"gap_{resampling}_{'cat' if companion is not None else 'plain'}.tif")
    warp_swath(
        values=values, lat=lat, lon=lon, output_path=path, epsg=epsg, resolution=resolution,
        bounds=bounds, resampling=resampling, scratch=scratch, categorical=companion,
    )
    dataset = gdal.Open(path)
    grid = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    return grid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("products", nargs="+")
    parser.add_argument("--product", default="lst")
    args = parser.parse_args()

    options = S3_PRODUCTS[args.product]
    member, variable = options["candidates"][0]
    geo_member, lat_name, lon_name = options["geolocation"]

    print(f"{args.product}: {variable} from {member}, geolocation {geo_member}")
    print(f"\n{'frame':34s} {'cells (avg)':>12s} {'gaps':>10s} {'gap %':>7s} {'largest run':>12s}")
    for product in args.products:
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="slstr-gaps-"))
        root = sen3_root(product)
        values = read(root, member, variable, scratch)
        lat = read(root, geo_member, lat_name, scratch)
        lon = read(root, geo_member, lon_name, scratch)

        epsg, resolution = choose_grid(
            lat, lon, ground_sampling_m=options.get("ground_sampling_m", SLSTR_GROUND_SAMPLING_M)
        )
        bounds = grid_bounds(lat, lon, epsg, resolution)
        companion = np.zeros(values.shape, dtype=np.float32)

        near = warped(values, lat, lon, scratch, epsg, resolution, bounds,
                      resampling="near", companion=companion)
        average = warped(values, lat, lon, scratch, epsg, resolution, bounds,
                         resampling="average", companion=None)

        filled = np.isfinite(average)
        gaps = filled & ~np.isfinite(near)
        # The longest run of gap cells along a row, which is what makes them
        # read as dashes rather than as noise.
        longest = 0
        for row in np.flatnonzero(gaps.any(axis=1)):
            line = gaps[row]
            run = 0
            for cell in line:
                run = run + 1 if cell else 0
                longest = max(longest, run)
        total = int(filled.sum())
        print(f"{pathlib.Path(product).name[:34]:34s} {total:12,} {int(gaps.sum()):10,} "
              f"{100 * gaps.sum() / max(1, total):6.2f}% {longest:12d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
