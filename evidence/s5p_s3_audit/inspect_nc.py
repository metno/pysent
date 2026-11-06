"""Print the layout of a NetCDF file: groups, arrays, dims, dtype, CF attributes.

Uses GDAL's multidimensional API only (no netCDF4/xarray), i.e. what pysent will
have in production.  Works on a local path or a /vsicurl/ /vsizip/ path.

    python3 inspect_nc.py FILE.nc [--grep PATTERN] [--attrs]
"""
from __future__ import annotations

import argparse
import re

from osgeo import gdal

gdal.UseExceptions()

_KEYS = ("units", "long_name", "standard_name", "_FillValue", "scale_factor", "add_offset",
         "valid_min", "valid_max", "valid_range", "multiplication_factor_to_convert_to_molecules_percm2",
         "flag_masks", "flag_values", "flag_meanings", "coordinates", "comment")


def walk(group, path=""):
    for name in group.GetMDArrayNames() or []:
        yield path + "/" + name, group.OpenMDArray(name)
    for sub in group.GetGroupNames() or []:
        yield from walk(group.OpenGroup(sub), path + "/" + sub)


def attrs(obj):
    out = {}
    for a in obj.GetAttributes() or []:
        out[a.GetName()] = a.Read()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--grep", default=".")
    ap.add_argument("--attrs", action="store_true", help="also print global/group attributes")
    args = ap.parse_args()
    ds = gdal.OpenEx(args.file, gdal.OF_MULTIDIM_RASTER)
    root = ds.GetRootGroup()
    if args.attrs:
        for k, v in attrs(root).items():
            print(f"# {k} = {str(v)[:160]}")
    rx = re.compile(args.grep)
    for path, arr in walk(root):
        if not rx.search(path):
            continue
        dims = ",".join(f"{d.GetName()}={d.GetSize()}" for d in arr.GetDimensions())
        at = attrs(arr)
        meta = {k: (str(at[k])[:70]) for k in _KEYS if k in at}
        # GDAL lifts units/_FillValue/scale_factor/add_offset out of the attribute list.
        if arr.GetUnit():
            meta["units"] = arr.GetUnit()
        nodata = arr.GetNoDataValueAsDouble()
        if nodata is not None:
            meta["_FillValue"] = repr(nodata)
        if arr.GetScale() not in (None, 1.0):
            meta["scale_factor"] = repr(arr.GetScale())
        if arr.GetOffset() not in (None, 0.0):
            meta["add_offset"] = repr(arr.GetOffset())
        dtype = gdal.GetDataTypeName(arr.GetDataType().GetNumericDataType())
        print(f"{path:70s} [{dims}] {dtype}")
        for k, v in meta.items():
            print(f"      {k} = {v}")


if __name__ == "__main__":
    main()
