"""Geometry metrics from --coverage runs: holes and spill of each method vs the exact pixel footprints.

    python3 s5p_compare.py DIR     # DIR holds polygons_*_cov.tif, geoloc-*_cov.tif
"""
import glob, os, sys
import numpy as np
from osgeo import gdal
gdal.UseExceptions()

d = sys.argv[1]
def load(p):
    ds = gdal.Open(p)  # keep the dataset alive while the band is read
    return np.isfinite(ds.GetRasterBand(1).ReadAsArray())
ref_p = glob.glob(os.path.join(d, "polygons_*_cov.tif"))[0]
ref = load(ref_p)
print(f"{'method':14s} {'footprint cells':>15s} {'method cells':>13s} {'holes':>8s} {'holes %':>8s} {'spill':>7s}")
for p in sorted(glob.glob(os.path.join(d, "*_cov.tif"))):
    m = load(p)
    holes = int((ref & ~m).sum()); spill = int((m & ~ref).sum())
    print(f"{os.path.basename(p).split('_')[0]:14s} {int(ref.sum()):15d} {int(m.sum()):13d} {holes:8d} {100*holes/ref.sum():8.2f} {spill:7d}")
