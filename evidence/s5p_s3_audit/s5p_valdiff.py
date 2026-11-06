"""Value agreement between regridding methods (masked runs, same QA threshold).

    python3 s5p_valdiff.py DIR QA_TAG       # e.g. DIR=.../val QA_TAG=qa75
"""
import os, sys
import numpy as np
from osgeo import gdal
gdal.UseExceptions()
d, tag = sys.argv[1], sys.argv[2]
def load(m):
    ds = gdal.Open(os.path.join(d, f"{m}_4326_{tag}.tif"))
    return ds.GetRasterBand(1).ReadAsArray()
ref = load("polygons-ss3")
print(f"reference: polygons-ss3, valid cells {int(np.isfinite(ref).sum())}")
for m in ("geoloc-near", "geoloc-avg"):
    a = load(m)
    both = np.isfinite(ref) & np.isfinite(a)
    only_ref = int((np.isfinite(ref) & ~np.isfinite(a)).sum()); only_a = int((np.isfinite(a) & ~np.isfinite(ref)).sum())
    diff = (a - ref)[both]
    scale = np.nanpercentile(np.abs(ref[both]), 95)
    print(f"{m:12s} common {int(both.sum())}  only-ref {only_ref}  only-method {only_a}  "
          f"mean|d| {np.mean(np.abs(diff)):.2e} ({100*np.mean(np.abs(diff))/scale:.1f}% of p95)  "
          f"p99|d| {np.percentile(np.abs(diff),99):.2e}  exact-equal {100*np.mean(diff==0):.1f}%")
