"""Render SLSTR L2 LST (Celsius, confidence/cloud mask) to a PNG via a geolocation-array warp.

    python3 s3_render_lst.py PRODUCT.SEN3 OUT.png [--epsg 32661] [--res 1000] [--vmin 0 --vmax 40] [--step 1]

Mask (PROPOSED default): reject LST fill, exception != 0, and confidence_in bits
  summary_cloud(16384) | unfilled(32) | summary_pointing(32768) | duplicate(512); keep only 'land' (8) pixels.
"""
import argparse, os, sys, tempfile, time
import numpy as np
from osgeo import gdal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import s3_warp
from s3_render import colorize, legend_strip, rgba_to_png

gdal.UseExceptions()
ap = argparse.ArgumentParser()
ap.add_argument("product"); ap.add_argument("out")
ap.add_argument("--epsg", type=int, default=32661); ap.add_argument("--res", type=float, default=1000.0)
ap.add_argument("--vmin", type=float, default=0.0); ap.add_argument("--vmax", type=float, default=40.0)
ap.add_argument("--step", type=int, default=1)
a = ap.parse_args()
t0 = time.time()
P = a.product
d_lst = gdal.Open(f'NETCDF:"{P}/LST_in.nc":LST'); b = d_lst.GetRasterBand(1)
raw = b.ReadAsArray(); scale, off, nd = b.GetScale(), b.GetOffset(), b.GetNoDataValue()
d_c = gdal.Open(f'NETCDF:"{P}/flags_in.nc":confidence_in'); conf = d_c.GetRasterBand(1).ReadAsArray()
d_e = gdal.Open(f'NETCDF:"{P}/LST_in.nc":exception'); exc = d_e.GetRasterBand(1).ReadAsArray()
bad = (conf & (16384 | 32 | 32768 | 512)) != 0
land = (conf & 8) != 0
keep = land & ~bad & (exc == 0) & (raw != nd)
celsius_all = raw.astype("float64") * scale + off - 273.15
v = celsius_all[keep]
print({"land%": round(100*land.mean(), 1), "cloud%": round(100*((conf & 16384) != 0).mean(), 1),
       "kept%": round(100*keep.mean(), 1), "C_p1/p50/p99": [round(float(x), 1) for x in np.percentile(v, [1, 50, 99])] if v.size else None})
tmp = tempfile.mkdtemp(prefix="lstrender_")
masked = np.where(keep, raw, int(nd)).astype("int16")
tif = os.path.join(tmp, "m.tif")
o = gdal.GetDriverByName("GTiff").Create(tif, raw.shape[1], raw.shape[0], 1, gdal.GDT_Int16)
o.GetRasterBand(1).WriteArray(masked); o.GetRasterBand(1).SetNoDataValue(nd); o = None
vrt = s3_warp.geoloc_vrt(tif, None, f"{P}/geodetic_in.nc", "longitude_in", "latitude_in", os.path.join(tmp, "in.vrt"), a.step)
ds = gdal.Warp(os.path.join(tmp, "w.tif"), vrt, format="GTiff", dstSRS=f"EPSG:{a.epsg}", xRes=a.res, yRes=a.res,
               resampleAlg="near", dstNodata=nd, errorThreshold=0.125)
arr = ds.GetRasterBand(1).ReadAsArray(); ok = arr != nd
val = np.where(ok, arr.astype("float64") * scale + off - 273.15, np.nan)
rgba = colorize(val, a.vmin, a.vmax, "temp", mask=ok)
ys, xs = np.where(ok)
rgba = rgba[ys.min():ys.max()+1, xs.min():xs.max()+1]
f = max(1, int(np.ceil(rgba.shape[1] / 2400))); rgba = rgba[::f, ::f]
strip = legend_strip(a.vmin, a.vmax, "temp", width=rgba.shape[1], height=22)
rgba_to_png(np.concatenate([rgba, np.zeros((6, rgba.shape[1], 4), np.uint8), strip]), a.out)
print({"warped": [ds.RasterXSize, ds.RasterYSize], "seconds": round(time.time()-t0, 1), "legend": f"{a.vmin}..{a.vmax} C, 'temp' map"})
