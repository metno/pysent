"""One line per 2-D (scanline x ground_pixel) variable in /PRODUCT of each file given, with units/scale/fill.

    python3 s5p_variables.py FILE.nc [FILE.nc ...] [--detailed]
"""
import sys
from osgeo import gdal
from inspect_nc import walk
gdal.UseExceptions()
detailed = "--detailed" in sys.argv
for path in [a for a in sys.argv[1:] if not a.startswith("--")]:
    root = gdal.OpenEx(path, gdal.OF_MULTIDIM_RASTER).GetRootGroup()
    print("##", path.split("/")[-1][:48])
    for p, arr in walk(root):
        if p.count("/") > (3 if detailed else 2) or "/METADATA" in p: continue
        if not detailed and "SUPPORT_DATA" in p: continue
        dims = [d.GetName() for d in arr.GetDimensions()]
        if dims[-2:] != ["scanline", "ground_pixel"] and dims[-3:] != ["scanline", "ground_pixel", "layer"]: continue
        at = {a.GetName(): a.Read() for a in arr.GetAttributes()}
        print(f"  {p.split('/PRODUCT/')[-1]:55s} {','.join(dims[1:]):32s} {gdal.GetDataTypeName(arr.GetDataType().GetNumericDataType()):8s} "
              f"{arr.GetUnit() or '-':14s} scale={arr.GetScale()} {str(at.get('long_name',''))[:50]}")
