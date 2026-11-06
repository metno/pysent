"""Render a Synergy VG1/V10 NDVI tile (regular lat/lon grid, read straight by GDAL) to a PNG.

    python3 s3_render_vg1.py PRODUCT.SEN3 OUT.png [--decimate 4] [--vmin -0.1 --vmax 0.9]
Mask (PROPOSED): NDVI fill (255) and SM status low bits 3 (cloud) / 1 (unfilled); NDVI = raw*0.004-0.08.
"""
import argparse, os, sys, time
import numpy as np
from osgeo import gdal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3_render import colorize, legend_strip, rgba_to_png
gdal.UseExceptions()
ap = argparse.ArgumentParser(); ap.add_argument("product"); ap.add_argument("out")
ap.add_argument("--decimate", type=int, default=4); ap.add_argument("--vmin", type=float, default=-0.1); ap.add_argument("--vmax", type=float, default=0.9)
a = ap.parse_args(); t0 = time.time()
d = gdal.Open(f'NETCDF:"{a.product}/NDVI.nc":NDVI'); b = d.GetRasterBand(1)
w, h = d.RasterXSize // a.decimate, d.RasterYSize // a.decimate
raw = b.ReadAsArray(buf_xsize=w, buf_ysize=h)
sm_ds = gdal.Open(f'NETCDF:"{a.product}/sm.nc":SM'); sm = sm_ds.GetRasterBand(1).ReadAsArray(buf_xsize=w, buf_ysize=h)
ok = (raw != 255) & ((sm & 3) == 0)
nd = raw.astype("float64") * b.GetScale() + b.GetOffset()
print({"valid%": round(100*(raw != 255).mean(), 1), "clear%": round(100*ok.mean(), 1), "cloud%": round(100*((sm & 3) == 3).mean(), 1),
       "ndvi_p2/50/98": [round(float(x), 2) for x in np.percentile(nd[ok], [2, 50, 98])]})
rgba = colorize(nd, a.vmin, a.vmax, "ndvi", mask=ok)
strip = legend_strip(a.vmin, a.vmax, "ndvi", width=w, height=22)
rgba_to_png(np.concatenate([rgba, np.zeros((6, w, 4), np.uint8), strip]), a.out)
print({"png": [w, h + 28], "seconds": round(time.time()-t0, 1), "legend": f"NDVI {a.vmin}..{a.vmax}, 'ndvi' map"})
