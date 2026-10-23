"""Small helpers shared by the S5P audit scripts (GDAL multidim API + numpy only)."""
from __future__ import annotations

import numpy as np
from osgeo import gdal

gdal.UseExceptions()


def open_root(path: str):
    return gdal.OpenEx(path, gdal.OF_MULTIDIM_RASTER).GetRootGroup()


def array(root, path: str):
    """Open an MDArray by '/PRODUCT/name' path."""
    group = root
    parts = [p for p in path.split("/") if p]
    for part in parts[:-1]:
        group = group.OpenGroup(part)
    return group.OpenMDArray(parts[-1])


def read(root, path: str, *, masked: bool = True) -> np.ndarray:
    """Read an array squeezed of the length-1 time axis, as float32, with
    ``_FillValue`` -> NaN and ``scale_factor``/``add_offset`` applied.

    GDAL's multidim reader returns *raw* packed values (qa_value is 0..100
    bytes), so the scaling has to be applied explicitly - a trap worth a test.
    """
    arr = array(root, path)
    data = arr.ReadAsArray()
    out = data.astype(np.float32 if data.dtype.kind != "f" else data.dtype)
    nodata = arr.GetNoDataValueAsDouble()
    if masked and nodata is not None:
        if data.dtype.kind == "f":
            out = np.where(np.isclose(data, nodata, rtol=1e-6, atol=0) | ~np.isfinite(data), np.nan, out)
        else:
            out = np.where(data == nodata, np.nan, out)
    scale, offset = arr.GetScale(), arr.GetOffset()
    if scale not in (None, 1.0):
        out = out * np.float32(scale)
    if offset not in (None, 0.0):
        out = out + np.float32(offset)
    return np.squeeze(out, axis=0) if out.ndim >= 3 and out.shape[0] == 1 else out


def km_between(lat1, lon1, lat2, lon2):
    """Great-circle distance in km (haversine, spherical Earth - fine for sizes)."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlmb = np.radians(((lon2 - lon1 + 180.0) % 360.0) - 180.0)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.sqrt(a))
