"""Compare ways of putting an S5P swath variable on a regular grid.

Methods (each run in its own process so peak RSS is attributable):

  geoloc-near   gdal.Warp(geoloc=True) over the 2-D centre lat/lon arrays, nearest
  geoloc-avg    same, resampling "average"
  polygons      exact pixel footprints from latitude_bounds/longitude_bounds,
                rasterised (last polygon wins on overlap)
  polygons-ss3  polygons at 3x finer grid, then block-averaged to the target grid
                (area-weighted mean of the footprints)

    python3 s5p_regrid.py FILE.nc METHOD --epsg 4326 --res 0.05 --out DIR
        [--var /PRODUCT/nitrogendioxide_tropospheric_column] [--qa-min 0.75] [--coverage]

``--coverage`` replaces the data by 1.0 on every pixel (no QA mask), to measure the
*geometry* (holes, spill) separately from the masking.

Prints one line of ``key=value`` pairs: wall seconds, peak RSS (MB), output cells.
"""
from __future__ import annotations

import argparse
import os
import resource
import time

import numpy as np
from osgeo import gdal, ogr, osr

from s5p_lib import array, open_root, read

gdal.UseExceptions()


def _srs(epsg):
    ref = osr.SpatialReference()
    ref.ImportFromEPSG(epsg)
    ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)  # x=lon/easting always
    return ref

NO2 = "/PRODUCT/nitrogendioxide_tropospheric_column"
GEO = "/PRODUCT/SUPPORT_DATA/GEOLOCATIONS"


def load(path, var, qa_min, coverage):
    root = open_root(path)
    lat = read(root, "/PRODUCT/latitude")
    lon = read(root, "/PRODUCT/longitude")
    val = read(root, var)
    if coverage:
        val = np.ones_like(val)
    else:
        # Compare the packed byte, not the scaled float: 0.75 is not exactly representable.
        qa_raw = np.squeeze(array(root, "/PRODUCT/qa_value").ReadAsArray())
        val = np.where(qa_raw > int(round(qa_min * 100)), val, np.nan).astype(np.float32)
    return root, lat.astype(np.float64), lon.astype(np.float64), val


def write_mem(name, data, nodata=np.nan):
    ds = gdal.GetDriverByName("GTiff").Create(name, data.shape[1], data.shape[0], 1, gdal.GDT_Float32)
    band = ds.GetRasterBand(1)
    band.WriteArray(data.astype(np.float32))
    if nodata is not None:
        band.SetNoDataValue(float(nodata))
    ds.FlushCache()
    ds = None


def grid_for(epsg, res, lat, lon):
    """Output bounds (minx, miny, maxx, maxy) covering the swath in the target CRS."""
    src, dst = _srs(4326), _srs(epsg)
    if epsg == 4326:
        wrapped = np.nanmax(np.abs(np.diff(lon, axis=1))) > 180 or np.nanmax(np.abs(np.diff(lon, axis=0))) > 180
        if wrapped:
            return (-180.0, float(np.floor(np.nanmin(lat) / res) * res), 180.0, float(np.ceil(np.nanmax(lat) / res) * res))
        return (float(np.floor(np.nanmin(lon) / res) * res), float(np.floor(np.nanmin(lat) / res) * res),
                float(np.ceil(np.nanmax(lon) / res) * res), float(np.ceil(np.nanmax(lat) / res) * res))
    ct = osr.CoordinateTransformation(src, dst)
    pts = np.array(ct.TransformPoints(np.column_stack([lon[::4, ::4].ravel(), lat[::4, ::4].ravel()]).tolist()))
    x, y = pts[:, 0], pts[:, 1]
    return (float(np.floor(x.min() / res) * res), float(np.floor(y.min() / res) * res),
            float(np.ceil(x.max() / res) * res), float(np.ceil(y.max() / res) * res))


def run_geoloc(lat, lon, val, epsg, res, bounds, resample, out):
    write_mem("/vsimem/val.tif", val)
    write_mem("/vsimem/lat.tif", lat, nodata=None)
    write_mem("/vsimem/lon.tif", lon, nodata=None)
    vrt = gdal.Translate("/vsimem/val.vrt", "/vsimem/val.tif", format="VRT")
    vrt.SetMetadata({
        "SRS": 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
               'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
        "X_DATASET": "/vsimem/lon.tif", "X_BAND": "1", "Y_DATASET": "/vsimem/lat.tif", "Y_BAND": "1",
        "PIXEL_OFFSET": "0", "LINE_OFFSET": "0", "PIXEL_STEP": "1", "LINE_STEP": "1",
    }, "GEOLOCATION")
    vrt.FlushCache()
    vrt = None
    gdal.Warp(out, "/vsimem/val.vrt", format="GTiff", dstSRS=f"EPSG:{epsg}", geoloc=True,
              outputBounds=bounds, xRes=res, yRes=res, resampleAlg=resample, dstNodata=float("nan"),
              outputType=gdal.GDT_Float32, creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])


def corner_rings(root, epsg):
    latb = read(root, f"{GEO}/latitude_bounds").astype(np.float64)
    lonb = read(root, f"{GEO}/longitude_bounds").astype(np.float64)
    return latb, lonb


def run_polygons(root, val, epsg, res, bounds, out, supersample=1):
    latb, lonb = corner_rings(root, epsg)
    src, dst = _srs(4326), _srs(epsg)
    ct = osr.CoordinateTransformation(src, dst)
    n = val.size
    pts = ct.TransformPoints(np.column_stack([lonb.reshape(-1), latb.reshape(-1)]).tolist())
    xy = np.asarray(pts)[:, :2].reshape(val.shape + (4, 2))

    ss = supersample
    fres = res / ss
    minx, miny, maxx, maxy = bounds
    ncols, nrows = int(round((maxx - minx) / fres)), int(round((maxy - miny) / fres))
    mem = gdal.GetDriverByName("MEM").Create("", ncols, nrows, 1, gdal.GDT_Float32)
    mem.SetGeoTransform((minx, fres, 0, maxy, 0, -fres))
    mem.SetProjection(dst.ExportToWkt())
    band = mem.GetRasterBand(1)
    band.Fill(float("nan"))
    band.SetNoDataValue(float("nan"))

    vect = ogr.GetDriverByName("Memory").CreateDataSource("p")
    layer = vect.CreateLayer("p", dst, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("v", ogr.OFTReal))
    defn = layer.GetLayerDefn()
    flat_val = val.reshape(-1)
    flat_xy = xy.reshape(n, 4, 2)
    # Feed the polygons in along-track order; on overlap the later scanline wins.
    for i in range(n):
        v = flat_val[i]
        if not np.isfinite(v):
            continue
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for k in range(4):
            ring.AddPoint_2D(float(flat_xy[i, k, 0]), float(flat_xy[i, k, 1]))
        ring.AddPoint_2D(float(flat_xy[i, 0, 0]), float(flat_xy[i, 0, 1]))
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        feat = ogr.Feature(defn)
        feat.SetGeometry(poly)
        feat.SetField("v", float(v))
        layer.CreateFeature(feat)
    gdal.RasterizeLayer(mem, [1], layer, options=["ATTRIBUTE=v"])
    arr = band.ReadAsArray()
    if ss > 1:
        arr = arr.reshape(nrows // ss, ss, ncols // ss, ss)
        valid = np.isfinite(arr)
        with np.errstate(invalid="ignore"):
            arr = np.where(valid.any(axis=(1, 3)), np.nansum(arr, axis=(1, 3)) / np.maximum(valid.sum(axis=(1, 3)), 1), np.nan)
    ds = gdal.GetDriverByName("GTiff").Create(out, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform((minx, res, 0, maxy, 0, -res))
    ds.SetProjection(dst.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(arr.astype(np.float32))
    ds.GetRasterBand(1).SetNoDataValue(float("nan"))
    ds.FlushCache()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("method", choices=["geoloc-near", "geoloc-avg", "polygons", "polygons-ss3"])
    ap.add_argument("--epsg", type=int, default=4326)
    ap.add_argument("--res", type=float, default=0.05)
    ap.add_argument("--var", default=NO2)
    ap.add_argument("--qa-min", type=float, default=0.75)
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    tag = "cov" if args.coverage else f"qa{int(args.qa_min * 100)}"
    out = os.path.join(args.out, f"{args.method}_{args.epsg}_{tag}.tif")

    t0 = time.perf_counter()
    root, lat, lon, val = load(args.file, args.var, args.qa_min, args.coverage)
    t_read = time.perf_counter() - t0
    bounds = grid_for(args.epsg, args.res, lat, lon)
    t1 = time.perf_counter()
    if args.method.startswith("geoloc"):
        run_geoloc(lat, lon, val, args.epsg, args.res, bounds, "near" if args.method == "geoloc-near" else "average", out)
    else:
        run_polygons(root, val, args.epsg, args.res, bounds, out, supersample=3 if args.method == "polygons-ss3" else 1)
    t_warp = time.perf_counter() - t1
    ds = gdal.Open(out)
    arr = ds.GetRasterBand(1).ReadAsArray()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"method={args.method} epsg={args.epsg} res={args.res} qa={tag} read_s={t_read:.2f} "
          f"warp_s={t_warp:.2f} peak_rss_mb={peak:.0f} cells={arr.size} valid={int(np.isfinite(arr).sum())} "
          f"size={ds.RasterXSize}x{ds.RasterYSize} out={out}")


if __name__ == "__main__":
    main()
