"""OLCI L1 EFR true colour (Oa08/Oa06/Oa04) at decimated size, radiance -> TOA reflectance.

    python3 s3_render_olci_l1.py BASE OUT.png [--decimate 8] [--gamma 0.6]
BASE may be a local .SEN3 dir or /vsizip//vsicurl/URL.zip/NAME.SEN3 (only the 3 bands + helpers are read).

reflectance = pi * L / (F0[band, detector] * cos(SZA)),  L = raw*scale (mW m-2 sr-1 nm-1),
F0 = instrument_data.solar_flux (long_name: 'seasonally corrected', so no Earth-Sun term is applied here),
SZA = tie_geometries.SZA interpolated across the 77 tie columns (stride 64).
"""
import argparse, os, sys, time
import numpy as np
from osgeo import gdal
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3_render import rgba_to_png
gdal.UseExceptions()
ap = argparse.ArgumentParser(); ap.add_argument("base"); ap.add_argument("out")
ap.add_argument("--decimate", type=int, default=8); ap.add_argument("--gamma", type=float, default=0.6)
a = ap.parse_args(); t0 = time.time(); B = a.base; D = a.decimate
def rd(f, v, dec=True):
    ds = gdal.Open(f'NETCDF:"{B}/{f}":{v}'); b = ds.GetRasterBand(1)
    arr = b.ReadAsArray(buf_xsize=ds.RasterXSize // D, buf_ysize=ds.RasterYSize // D) if dec else b.ReadAsArray()
    return arr, b.GetScale() or 1.0, b.GetOffset() or 0.0, b.GetNoDataValue(), ds.RasterXSize, ds.RasterYSize
bands = {"R": "Oa08", "G": "Oa06", "B": "Oa04"}
det, *_ , W, H = rd("instrument_data.nc", "detector_index")
h, w = det.shape
szat, s, o, _, tc, tr = rd("tie_geometries.nc", "SZA", dec=False)
sza = szat.astype("float64") * s + o
rows = (np.arange(h) * D + D // 2).clip(0, sza.shape[0] - 1)
xs = np.arange(tc) * 64
cols = np.arange(w) * D + D // 2
cosz = np.empty((h, w))
for i, r in enumerate(rows):
    cosz[i] = np.cos(np.radians(np.interp(cols, xs, sza[r])))
fl = gdal.Open(f'NETCDF:"{B}/instrument_data.nc":solar_flux'); F0 = fl.GetRasterBand(1)  # bands x detectors (3700)
F0 = fl.ReadAsArray() if fl.RasterCount == 0 else None
fl_arr = gdal.Open(f'NETCDF:"{B}/instrument_data.nc":solar_flux').ReadAsArray()
rgb = []; info = {}
for ch, name in bands.items():
    raw, sc, off, nd, *_ = rd(f"{name}_radiance.nc", f"{name}_radiance")
    band_idx = int(name[2:]) - 1
    f0 = fl_arr[band_idx][np.clip(det, 0, fl_arr.shape[1] - 1)]
    L = raw.astype("float64") * sc + off
    refl = np.pi * L / (f0 * np.maximum(cosz, 0.05))
    refl[(raw == nd) | (det < 0)] = np.nan
    info[name] = [round(float(x), 3) for x in np.nanpercentile(refl, [1, 50, 99])]
    rgb.append(refl)
valid = np.all([np.isfinite(r) for r in rgb], axis=0)
out = np.zeros((h, w, 3), np.uint8)
for i, r in enumerate(rgb):
    v = np.clip(np.nan_to_num(r) / 0.6, 0, 1) ** a.gamma      # fixed 0..0.6 reflectance window, gamma
    out[..., i] = (v * 255).round().astype(np.uint8)
bg = np.array([235, 238, 240], np.uint8)
out[~valid] = bg
rgba = np.concatenate([out, np.where(valid, 255, 0).astype(np.uint8)[..., None]], axis=2)
rgba_to_png(rgba, a.out)
print({"reflectance_p1/p50/p99": info, "valid%": round(100 * valid.mean(), 1), "png": [w, h], "seconds": round(time.time() - t0, 1),
       "stretch": f"reflectance 0..0.6, gamma {a.gamma}"})
