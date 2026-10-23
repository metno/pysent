"""What is actually in an S5P L2 granule: extents, antimeridian/pole behaviour,
pixel size, fill and quality statistics.

    python3 s5p_probe.py FILE.nc [VAR]       # VAR defaults to the NO2 tropospheric column
"""
from __future__ import annotations

import sys

import numpy as np

from s5p_lib import km_between, open_root, read

DEFAULT_VAR = "/PRODUCT/nitrogendioxide_tropospheric_column"


def main(path: str, var: str) -> None:
    root = open_root(path)
    lat = read(root, "/PRODUCT/latitude")
    lon = read(root, "/PRODUCT/longitude")
    qa = read(root, "/PRODUCT/qa_value")
    val = read(root, var)
    latb = read(root, "/PRODUCT/SUPPORT_DATA/GEOLOCATIONS/latitude_bounds")
    lonb = read(root, "/PRODUCT/SUPPORT_DATA/GEOLOCATIONS/longitude_bounds")
    delta = read(root, "/PRODUCT/delta_time")
    print(f"file                : {path.split('/')[-1]}")
    print(f"shape (scan,pixel)  : {lat.shape}   pixels: {lat.size}")
    print(f"lat range           : {np.nanmin(lat):.3f} .. {np.nanmax(lat):.3f}")
    print(f"lon range (raw)     : {np.nanmin(lon):.3f} .. {np.nanmax(lon):.3f}")

    # Antimeridian: a jump of >180 deg between neighbouring pixel centres.
    jump_across = np.nanmax(np.abs(np.diff(lon, axis=1)))
    jump_along = np.nanmax(np.abs(np.diff(lon, axis=0)))
    print(f"max |dlon| neighbours: across-track {jump_across:.1f}  along-track {jump_along:.1f}"
          f"   -> crosses antimeridian: {max(jump_across, jump_along) > 180}")

    # Does the pole lie inside the swath?  Longitude spans all values near the pole.
    near_pole = np.nanmax(lat) > 88
    print(f"max latitude {np.nanmax(lat):.2f}: pole within ~2 deg: {near_pole}")

    # Pixel size from the corners (corner order: GEOLOCATIONS/latitude_bounds[..., 4]).
    across = km_between(latb[..., 0], lonb[..., 0], latb[..., 1], lonb[..., 1])
    along = km_between(latb[..., 1], lonb[..., 1], latb[..., 2], lonb[..., 2])
    print(f"pixel edge 0-1 (km) : median {np.nanmedian(across):.1f} min {np.nanmin(across):.1f} max {np.nanmax(across):.1f}")
    print(f"pixel edge 1-2 (km) : median {np.nanmedian(along):.1f} min {np.nanmin(along):.1f} max {np.nanmax(along):.1f}")

    # Centres vs corners: are the corners self-consistent with the centres?
    cen_lat = np.nanmean(latb, axis=-1)
    print(f"|centre - mean(corners)| lat max: {np.nanmax(np.abs(cen_lat - lat)):.4f} deg")

    print(f"delta_time (ms)     : {np.nanmin(delta):.0f} .. {np.nanmax(delta):.0f}  "
          f"({(np.nanmax(delta) - np.nanmin(delta)) / 1000:.1f} s), "
          f"step {np.nanmedian(np.diff(delta[:, 0] if delta.ndim == 2 else delta)):.0f} ms")

    fill = np.isnan(val)
    print(f"{var.split('/')[-1]}: fill {fill.mean() * 100:.1f} %")
    for thr in (0.0, 0.5, 0.75):
        keep = ~fill & (qa > thr) if thr else ~fill
        pct = np.nanpercentile(val[keep], [1, 5, 50, 95, 99]) if keep.any() else [np.nan] * 5
        print(f"  qa>{thr:<4} : kept {keep.mean() * 100:5.1f} %   p1/5/50/95/99 = "
              + " ".join(f"{p:.3e}" for p in pct))
    print(f"qa histogram (0,.1,..1): {np.histogram(qa[~np.isnan(qa)], bins=np.linspace(0, 1.0001, 11))[0].tolist()}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VAR)
