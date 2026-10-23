"""Positional error (metres) of cheaper geolocation models against the full lat/lon arrays.

Truth = the product's own full-resolution (DEM-corrected for OLCI) latitude/longitude.
For a random sample of pixels it reports mean / p95 / max error of:

* tie-point grid, linear interpolation across the tie columns (numpy)
* GDAL geolocation arrays at PIXEL_STEP/LINE_STEP = 1, 4, 16, 64 (GDAL Transformer)
* GDAL thin-plate-spline GCP model built from the tie-point grid at several strides

    python3 s3_positional_error.py GEO_NC TIE_NC [--x longitude --y latitude] [--ac 64]
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

import numpy as np
from osgeo import gdal

sys.path.insert(0, os.path.dirname(__file__))
from s3_warp import _nc, gcp_vrt, geoloc_vrt  # noqa: E402

gdal.UseExceptions()


def metres(lat_a, lon_a, lat_b, lon_b):
    dlat = (lat_a - lat_b) * 111195.0
    dlon = (lon_a - lon_b) * 111195.0 * np.cos(np.radians((lat_a + lat_b) / 2))
    return np.hypot(dlat, dlon)


def stats(label, err, extra=""):
    print(f"  {label:44s} mean {err.mean():8.1f} m   p95 {np.percentile(err, 95):8.1f} m   max {err.max():8.1f} m  {extra}")


def read_scaled(path, var):
    ds = gdal.Open(_nc(path, var))
    b = ds.GetRasterBand(1)
    return b.ReadAsArray().astype("float64") * (b.GetScale() or 1.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("geo_nc")
    ap.add_argument("tie_nc")
    ap.add_argument("--x", default="longitude")
    ap.add_argument("--y", default="latitude")
    ap.add_argument("--tx", default="longitude")
    ap.add_argument("--ty", default="latitude")
    ap.add_argument("--ac", type=int, default=64, help="across-track tie-point subsampling")
    ap.add_argument("--var-nc", help="any variable file of the same grid (for the VRT shell)")
    ap.add_argument("--subds", help="its variable name")
    ap.add_argument("--n", type=int, default=20000)
    args = ap.parse_args()

    lon = read_scaled(args.geo_nc, args.x)
    lat = read_scaled(args.geo_nc, args.y)
    rows, cols = lat.shape
    print(f"full lat/lon array: {cols} x {rows} = {rows * cols / 1e6:.1f} Mpx; "
          f"float64 pair = {2 * rows * cols * 8 / 1e6:.0f} MB, int32 pair = {2 * rows * cols * 4 / 1e6:.0f} MB")
    rng = np.random.default_rng(0)
    rr = rng.integers(0, rows, args.n)
    cc = rng.integers(0, cols, args.n)
    tlat, tlon = lat[rr, cc], lon[rr, cc]

    # --- tie-point grid, numpy linear interpolation across columns
    tlon_t = read_scaled(args.tie_nc, args.tx)
    tlat_t = read_scaled(args.tie_nc, args.ty)
    trows, tcols = tlat_t.shape
    print(f"tie grid: {tcols} cols x {trows} rows (ac stride {args.ac}); covers image col 0..{(tcols - 1) * args.ac}")
    xs_t = np.arange(tcols) * args.ac
    ilat = np.empty(args.n)
    ilon = np.empty(args.n)
    for k in range(args.n):
        r = min(rr[k], trows - 1)
        ilat[k] = np.interp(cc[k], xs_t, tlat_t[r])
        ilon[k] = np.interp(cc[k], xs_t, tlon_t[r])
    e = metres(ilat, ilon, tlat, tlon)
    stats("tie-point linear interpolation", e)
    # same restricted to columns that are exact tie columns -> pure DEM/altitude term
    exact = (cc % args.ac) == 0
    if exact.any():
        stats("  ...at the tie columns themselves", e[exact])

    if not (args.var_nc and args.subds):
        return
    tmp = tempfile.mkdtemp(prefix="s3pos_")
    pts = [(float(c) + 0.5, float(r) + 0.5, 0.0) for r, c in zip(rr, cc)]

    def eval_transformer(vrt, method):
        ds = gdal.Open(vrt)
        t = gdal.Transformer(ds, None, [f"METHOD={method}"])
        out, ok = t.TransformPoints(False, pts)
        out = np.array(out)
        ok = np.array(ok, dtype=bool)
        return metres(out[:, 1], out[:, 0], tlat, tlon), ok

    for step in (1, 4, 16, 64):
        d = os.path.join(tmp, f"g{step}")
        os.makedirs(d)
        t0 = time.time()
        vrt = geoloc_vrt(args.var_nc, args.subds, args.geo_nc, args.x, args.y, os.path.join(d, "in.vrt"), step)
        e, ok = eval_transformer(vrt, "GEOLOC_ARRAY")
        stats(f"GDAL geolocation arrays, step {step}", e[ok], f"(ok {ok.mean():.3f}, {time.time() - t0:.1f}s)")
    for sr, sc in ((64, 4), (16, 4), (16, 1), (4, 1)):
        d = os.path.join(tmp, f"t{sr}_{sc}")
        os.makedirs(d)
        t0 = time.time()
        vrt, n = gcp_vrt(args.var_nc, args.subds, args.tie_nc, args.tx, args.ty, os.path.join(d, "in.vrt"),
                         sr, sc, args.ac)
        e, ok = eval_transformer(vrt, "GCP_TPS")
        stats(f"GCP thin-plate spline, stride r{sr} c{sc} ({n} GCPs)", e[ok], f"(ok {ok.mean():.3f}, {time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
