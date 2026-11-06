"""Candidate warp paths for Sentinel-3 swath variables, one variant per process.

A "variant" is run in its own interpreter so ``ru_maxrss`` is that variant's
peak, not the high-water mark of everything before it.  The driver
(``s3_bench.sh``-style loop, or call ``bench()``) alternates variants.

    python3 s3_warp.py geoloc  VAR_NC SUBDS GEO_NC X_VAR Y_VAR OUT.tif EPSG RES [--xy-scale 1e-6]
    python3 s3_warp.py gcp     VAR_NC SUBDS TIE_NC X_VAR Y_VAR OUT.tif EPSG RES [--stride-r 64 --stride-c 4]
    python3 s3_warp.py direct  VAR_NC SUBDS - - - OUT.tif EPSG RES

Prints one JSON line: wall seconds, peak RSS MB, output size, output extent.

geoloc : GDAL GEOLOCATION metadata domain on a VRT of the variable, ``gdalwarp -geoloc``
gcp    : GCPs taken from the tie-point grid, thin-plate-spline warp
direct : plain gdal.Warp of an already georeferenced grid (Synergy VG1/V10)
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import tempfile
import time

import numpy as np
from osgeo import gdal, osr

gdal.UseExceptions()


def _nc(path: str, var: str | None = None) -> str:
    return f'NETCDF:"{path}":{var}' if var else path


def _unscaled_copy(nc: str, var: str, out_tif: str, step: int = 1) -> str:
    """GDAL's GEOLOCATION domain does NOT apply scale_factor (S3 lat/lon are int32 * 1e-6):
    a raw-integer array makes the warp try to allocate an absurd output (std::length_error).
    So write an unscaled Float32 copy first (Float32 keeps ~0.4 m at these magnitudes)."""
    if step == 1:
        gdal.Translate(out_tif, _nc(nc, var), unscale=True, outputType=gdal.GDT_Float32,
                       creationOptions=["TILED=YES"])
        return out_tif
    # decimated geolocation array: sample i corresponds to image pixel i * step (PIXEL_STEP/LINE_STEP)
    src = gdal.Open(_nc(nc, var))
    b = src.GetRasterBand(1)
    arr = b.ReadAsArray()[::step, ::step].astype("float32") * np.float32(b.GetScale() or 1.0)
    out = gdal.GetDriverByName("GTiff").Create(out_tif, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    out.GetRasterBand(1).WriteArray(arr)
    out = None
    return out_tif


def geoloc_vrt(var_nc: str, subds: str, geo_nc: str, x_var: str, y_var: str, vrt_path: str, step: int = 1) -> str:
    base = os.path.dirname(vrt_path)
    gx = _unscaled_copy(geo_nc, x_var, os.path.join(base, "x.tif"), step)
    gy = _unscaled_copy(geo_nc, y_var, os.path.join(base, "y.tif"), step)
    src = gdal.Open(_nc(var_nc, subds))
    vrt = gdal.GetDriverByName("VRT").CreateCopy(vrt_path, src)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    vrt.SetMetadata({
        "SRS": srs.ExportToWkt(),
        "X_DATASET": gx, "X_BAND": "1",
        "Y_DATASET": gy, "Y_BAND": "1",
        "GEOREFERENCING_CONVENTION": "PIXEL_CENTER",  # S3/S5P lat/lon are pixel centres; GDAL default is TOP_LEFT_CORNER
        # With PIXEL_STEP/LINE_STEP = N GDAL treats a sample as the CENTRE of an N x N block, so samples
        # taken at pixel i*N need an offset of -(N-1)/2 (measured: step 4 -> 70 m mean error, 0 -> 600 m).
        "PIXEL_OFFSET": str(-(step - 1) / 2), "LINE_OFFSET": str(-(step - 1) / 2),
        "PIXEL_STEP": str(step), "LINE_STEP": str(step),
    }, "GEOLOCATION")
    vrt.FlushCache()
    return vrt_path


def gcp_vrt(var_nc: str, subds: str, tie_nc: str, x_var: str, y_var: str, vrt_path: str,
            stride_r: int, stride_c: int, ac_factor: int = 64) -> tuple[str, int]:
    """GCPs from the tie-point grid: tie row r is image row r; tie col c is image col c * ac_factor."""
    dx, dy = gdal.Open(_nc(tie_nc, x_var)), gdal.Open(_nc(tie_nc, y_var))
    bx, by = dx.GetRasterBand(1), dy.GetRasterBand(1)
    tx = bx.ReadAsArray().astype("float64") * (bx.GetScale() or 1.0)
    ty = by.ReadAsArray().astype("float64") * (by.GetScale() or 1.0)
    src = gdal.Open(_nc(var_nc, subds))
    rows, cols = tx.shape
    rs = sorted(set(range(0, rows, stride_r)) | {rows - 1})  # always include the last row / column
    cs = sorted(set(range(0, cols, stride_c)) | {cols - 1})
    gcps = [gdal.GCP(float(tx[r, c]), float(ty[r, c]), 0.0, float(c * ac_factor) + 0.5, float(r) + 0.5)
            for r in rs for c in cs]
    vrt = gdal.GetDriverByName("VRT").CreateCopy(vrt_path, src)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    vrt.SetGCPs(gcps, wgs.ExportToWkt())
    vrt.FlushCache()
    return vrt_path, len(gcps)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["geoloc", "gcp", "direct"])
    ap.add_argument("var_nc")
    ap.add_argument("subds")
    ap.add_argument("geo_nc")
    ap.add_argument("x_var")
    ap.add_argument("y_var")
    ap.add_argument("out")
    ap.add_argument("epsg", type=int)
    ap.add_argument("res", type=float, help="output pixel size in target units; 0 = let GDAL choose")
    ap.add_argument("--stride-r", type=int, default=64)
    ap.add_argument("--stride-c", type=int, default=4)
    ap.add_argument("--resample", default="near")
    ap.add_argument("--threads", default="1")
    ap.add_argument("--geoloc-step", type=int, default=1)
    ap.add_argument("--bounds", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    help="explicit output bounds (decimated geoloc arrays can yield a silently truncated auto extent)")
    args = ap.parse_args()

    gdal.SetCacheMax(256 * 1024 * 1024)  # 256 MB, the pysent interactive lesson (GDAL cache per child)
    gdal.SetConfigOption("GDAL_NUM_THREADS", args.threads)
    tmp = tempfile.mkdtemp(prefix="s3warp_")
    vrt = os.path.join(tmp, "in.vrt")
    extra = {}
    t0 = time.time()
    if args.mode == "geoloc":
        geoloc_vrt(args.var_nc, args.subds, args.geo_nc, args.x_var, args.y_var, vrt, args.geoloc_step)
        extra["geoloc_step"] = args.geoloc_step
    elif args.mode == "gcp":
        _, n = gcp_vrt(args.var_nc, args.subds, args.geo_nc, args.x_var, args.y_var, vrt,
                       args.stride_r, args.stride_c)
        extra["gcps"] = n
    else:
        vrt = _nc(args.var_nc, args.subds)
    t_setup = time.time() - t0
    opts = dict(
        format="GTiff", dstSRS=f"EPSG:{args.epsg}", resampleAlg=args.resample,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256"],
        multithread=False, errorThreshold=0.125,
    )
    if args.res:
        opts["xRes"] = opts["yRes"] = args.res
    if args.bounds:
        opts["outputBounds"] = tuple(args.bounds)
    if args.mode == "geoloc":
        opts["geoloc"] = True
    if args.mode == "gcp":
        opts["tps"] = True
    ds = gdal.Warp(args.out, vrt, **opts)
    gt = ds.GetGeoTransform()
    out = {
        "mode": args.mode, "epsg": args.epsg, "res": args.res, "seconds": round(time.time() - t0, 2),
        "setup_seconds": round(t_setup, 2),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 0),
        "out_size_px": [ds.RasterXSize, ds.RasterYSize], "out_mb": round(os.path.getsize(args.out) / 1e6, 2),
        "gt": [round(v, 6) for v in gt], **extra,
    }
    ds = None
    print(json.dumps(out))


if __name__ == "__main__":
    main()
