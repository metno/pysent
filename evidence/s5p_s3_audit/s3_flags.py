"""Print every flag name/mask of a NetCDF flag variable (and optionally its frequency).

    python3 s3_flags.py FILE.nc VARIABLE [--freq]

``--freq`` reads the variable at 1/8 decimation and prints, per flag, the % of
pixels that carry it (nearest-neighbour decimation; fine for a frequency table).
"""
from __future__ import annotations

import argparse

import numpy as np
from osgeo import gdal

gdal.UseExceptions()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("var")
    ap.add_argument("--freq", action="store_true")
    args = ap.parse_args()
    ds = gdal.OpenEx(args.file, gdal.OF_MULTIDIM_RASTER)
    arr = ds.GetRootGroup().OpenMDArray(args.var)
    at = {a.GetName(): a.Read() for a in arr.GetAttributes()}
    masks = at.get("flag_masks", at.get("flag_values"))
    names = str(at.get("flag_meanings", "")).split()
    masks = [int(m) for m in (masks if isinstance(masks, (list, tuple)) else [masks])]
    print(f"{args.var}: dtype {gdal.GetDataTypeName(arr.GetDataType().GetNumericDataType())}, "
          f"{len(masks)} masks, {len(names)} names")
    data = None
    if args.freq:
        dims = arr.GetDimensions()
        shape = [d.GetSize() for d in dims]
        step = [8] * len(shape)
        count = [-(-n // 8) for n in shape]  # ceil(n / 8)
        data = arr.ReadAsArray(array_start_idx=[0] * len(shape), count=count, array_step=step)
        data = np.asarray(data).astype("uint64")
    for name, mask in zip(names, masks):
        pct = f"  {100.0 * ((data & np.uint64(mask)) != 0).mean():6.2f} %" if data is not None else ""
        print(f"  {mask:>12d}  0x{mask:08x}  bit {mask.bit_length() - 1:>2d}  {name}{pct}")


if __name__ == "__main__":
    main()
