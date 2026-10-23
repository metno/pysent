"""Render OLCI WFR chlorophyll (log colour scale, WQSF water-only mask) to a PNG.

    python3 s3_render_wfr.py PRODUCT.SEN3 OUT.png [--epsg 32661] [--res 600] [--var chl_nn:CHL_NN]
                              [--vmin 0.01 --vmax 20] [--step 16]

Pipeline (what a pysent pipeline would do):
  1. read the packed Byte/UInt16 variable and WQSF with the classic NetCDF driver
  2. mask in SWATH space (before resampling, so nodata cannot bleed into neighbours)
  3. unpack  value = 10 ** (raw * scale_factor + add_offset)   (WFR log10 packing)
  4. geolocation-array warp (decimated, PIXEL_CENTER, offset -(step-1)/2) to the target CRS
  5. colour: log scale + 'chl' map; masked pixels transparent
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

import numpy as np
from osgeo import gdal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s3_warp  # noqa: E402
from s3_render import colorize, legend_strip, rgba_to_png  # noqa: E402

gdal.UseExceptions()

#: WQSF bit positions (read from the file's flag_meanings/flag_masks; baseline 004)
WQSF_BITS = {"INVALID": 0, "WATER": 1, "LAND": 2, "CLOUD": 3, "SNOW_ICE": 4, "CLOUD_AMBIGUOUS": 23,
             "CLOUD_MARGIN": 24, "SATURATED": 11, "HIGHGLINT": 13, "WHITECAPS": 14, "ADJAC": 15,
             "AC_FAIL": 18, "OCNN_FAIL": 20, "CHLOR_A_FAIL": 19, "KD_FAIL": 21, "CLOUD_SHADOW": 31, "TURBID_ATM": 22}

#: PROPOSED default exclusion list for chlorophyll (judgement, not copied from the product notice)
DEFAULT_REJECT = ["INVALID", "LAND", "CLOUD", "CLOUD_AMBIGUOUS", "CLOUD_MARGIN", "SNOW_ICE", "SATURATED",
                  "HIGHGLINT", "WHITECAPS", "AC_FAIL", "CLOUD_SHADOW"]


def bits(w: np.ndarray, names) -> np.ndarray:
    out = np.zeros(w.shape, bool)
    for n in names:
        out |= ((w >> np.uint64(WQSF_BITS[n])) & np.uint64(1)).astype(bool)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("product")
    ap.add_argument("out")
    ap.add_argument("--epsg", type=int, default=32661)
    ap.add_argument("--res", type=float, default=600.0)
    ap.add_argument("--var", default="chl_nn:CHL_NN")
    ap.add_argument("--vmin", type=float, default=0.01)
    ap.add_argument("--vmax", type=float, default=20.0)
    ap.add_argument("--step", type=int, default=16)
    ap.add_argument("--reject", default=",".join(DEFAULT_REJECT))
    a = ap.parse_args()

    t0 = time.time()
    fname, var = a.var.split(":")
    src = gdal.Open(f'NETCDF:"{a.product}/{fname}.nc":{var}')
    band = src.GetRasterBand(1)
    raw = band.ReadAsArray()
    scale, offset, nodata = band.GetScale(), band.GetOffset(), band.GetNoDataValue()
    wq_ds = gdal.Open(f'NETCDF:"{a.product}/wqsf.nc":WQSF')  # keep the dataset alive while reading its band
    wq = wq_ds.GetRasterBand(1).ReadAsArray()
    water = bits(wq, ["WATER"])
    bad = bits(wq, a.reject.split(","))
    keep = water & ~bad & (raw != nodata)
    stats = {
        "pixels": int(raw.size), "water%": round(100 * water.mean(), 2), "rejected_of_water%": round(100 * (bad & water).sum() / max(water.sum(), 1), 2),
        "kept%": round(100 * keep.mean(), 2),
    }
    vals = 10 ** (raw[keep].astype("float64") * scale + offset)
    stats["chl_mg_m3_p1/p50/p99"] = [round(float(x), 3) for x in np.percentile(vals, [1, 50, 99])] if vals.size else None

    tmp = tempfile.mkdtemp(prefix="wfrrender_")
    masked = np.where(keep, raw, int(nodata)).astype(raw.dtype)
    tif = os.path.join(tmp, "masked.tif")
    dt = gdal.GDT_Byte if raw.dtype == np.uint8 else gdal.GDT_UInt16
    out = gdal.GetDriverByName("GTiff").Create(tif, raw.shape[1], raw.shape[0], 1, dt)
    ob = out.GetRasterBand(1)
    ob.WriteArray(masked)
    ob.SetNoDataValue(nodata)
    out = None
    vrt = s3_warp.geoloc_vrt(tif, None, os.path.join(a.product, "geo_coordinates.nc"), "longitude", "latitude",
                             os.path.join(tmp, "in.vrt"), a.step)
    warped = os.path.join(tmp, "warped.tif")
    ds = gdal.Warp(warped, vrt, format="GTiff", dstSRS=f"EPSG:{a.epsg}", xRes=a.res, yRes=a.res,
                   resampleAlg="near", dstNodata=nodata, errorThreshold=0.125)
    arr = ds.GetRasterBand(1).ReadAsArray()
    stats["warped_size"] = [ds.RasterXSize, ds.RasterYSize]
    ok = arr != nodata
    val = np.where(ok, 10 ** (arr.astype("float64") * scale + offset), np.nan)
    rgba = colorize(val, a.vmin, a.vmax, "chl", mask=ok, log=True)
    # trim to the data extent and decimate for a viewable PNG
    ys, xs = np.where(ok)
    if ys.size:
        rgba = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    f = max(1, int(np.ceil(rgba.shape[1] / 2400)))
    rgba = rgba[::f, ::f]
    strip = legend_strip(a.vmin, a.vmax, "chl", width=rgba.shape[1], height=22)
    rgba_to_png(np.concatenate([rgba, np.zeros((6, rgba.shape[1], 4), np.uint8), strip], axis=0), a.out)
    stats["png_size"] = [rgba.shape[1], rgba.shape[0] + 28]
    stats["seconds"] = round(time.time() - t0, 1)
    stats["legend"] = f"log10 scale {a.vmin} .. {a.vmax} mg/m3, 'chl' map, left=low"
    print(stats)


if __name__ == "__main__":
    main()
