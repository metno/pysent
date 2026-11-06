# Corrupt-input check: expects /out/corrupt.zip (an S2 zip with bytes zeroed inside B04_10m.jp2). See PLANNING_bulk_processing.md, finding B7.
import os
from pathlib import Path
import numpy as np, rasterio
from pysent.s2 import S2_DEFAULT_PRODUCTS, process_sentinel_s2_safe
p = "true_color_vegetation"
try:
    r = process_sentinel_s2_safe(input_dataset="/out/corrupt.zip", output_dir=Path("/out/res"),
        product_bands={p: S2_DEFAULT_PRODUCTS[p]}, output_names={p: "o.tif"},
        processing_options={"histogram_stretch": True})
    print("RETURNED normally:", r[0]["path"])
    with rasterio.open(r[0]["path"]) as src:
        red = src.read(1, out_shape=(1098, 1098))
        print("red band zero fraction:", float(np.mean(red == 0)))
except Exception as e:
    print("RAISED", type(e).__name__, str(e)[:200])
print("left in output dir:", sorted(os.listdir("/out/res")))
