"""How much variation is left in the quality value once the threshold has run.

A quality-weighted mean only differs from a plain mean to the extent the
weights differ. If the surviving quality values are nearly all the same, the
weighting is arithmetic that changes nothing.
"""
import pathlib, sys
import numpy as np
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))
from osgeo import gdal
gdal.UseExceptions()
from pysent._swath import open_root, read_array, read_raw_array

NO2 = "/PRODUCT/nitrogendioxide_tropospheric_column"
for path in sorted(pathlib.Path(sys.argv[1]).glob("S5P_*NO2*.nc"))[:4]:
    root = open_root(str(path))
    values = read_array(root, NO2)
    qa = read_raw_array(root, "/PRODUCT/qa_value")
    for threshold in (50, 75):
        keep = (qa > threshold) & np.isfinite(values)
        if keep.sum() < 100:
            print(f"{path.name[22:40]} qa>{threshold}: nothing"); continue
        surviving = qa[keep]
        unique, counts = np.unique(surviving, return_counts=True)
        top = sorted(zip(counts, unique), reverse=True)[:3]
        share = ", ".join(f"{v/100:.2f}={100*c/surviving.size:.0f}%" for c, v in top)
        print(f"{path.name[22:40]} qa>{threshold:>2}: kept {100*keep.mean():5.1f}%  "
              f"weights mean {surviving.mean()/100:.3f} sd {surviving.std()/100:.3f}  top: {share}")
