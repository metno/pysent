"""Where does the correction stop being trustworthy?

The whole OLCI holding sits at solar zenith 63 to 79 degrees, and two frames
showed the land residual going from 0.013 at 71 degrees to 0.094 at 79.6. Two
points cannot place a threshold, so this runs the correction across a sample of
the matched triples and reports the residual against ESA's own product as a
function of solar zenith - which is what a per-pixel quality band needs in
order to say anything honest.

    python3 sweep.py [--count 20] [--decimate 8] [--platform S3A]

Each row is one acquisition: its median solar zenith, how much of the frame is
usable, and the median residual per band over clear land (against Synergy
surface reflectance) and over clear water (against the water-leaving
reflectance). Frames with too few usable pixels to mean anything are reported
as skipped rather than averaged in.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import tempfile

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from osgeo import gdal  # noqa: E402

gdal.UseExceptions()

from budget import BANDS, _toa, level2_reflectance, tie_field  # noqa: E402
from pairs import collect  # noqa: E402

from pysent._atmosphere import (  # noqa: E402
    gas_transmittance,
    pixel_pressure,
    platform_of,
    rayleigh_terms,
    relative_azimuth,
    surface_from_toa,
)
from pysent._olci_l1 import (  # noqa: E402
    GEOMETRY_MEMBER,
    METEO_MEMBER,
    quality_mask,
    radiance_member,
)
from pysent._sen3 import (  # noqa: E402
    extract_member,
    find_member,
    open_sen3_variable,
    read_packed,
    sen3_root,
)

#: Below this many usable pixels a median says nothing, so the frame is skipped.
MINIMUM_PIXELS = 20_000


def corrected_frame(l1_path, decimate):
    """Surface reflectance for the three bands, plus the masks and geometry."""
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="ac-sweep-"))
    root = sen3_root(str(l1_path))
    platform = platform_of(str(l1_path))

    member, variable = radiance_member(BANDS[0])
    probe = open_sen3_variable(extract_member(root, find_member(root, member), scratch), variable)
    columns, rows = probe.RasterXSize, probe.RasterYSize
    probe = None
    shape = (max(1, rows // decimate), max(1, columns // decimate))

    sza = tie_field(root, GEOMETRY_MEMBER, "SZA", shape, scratch)
    oza = tie_field(root, GEOMETRY_MEMBER, "OZA", shape, scratch)
    saa = tie_field(root, GEOMETRY_MEMBER, "SAA", shape, scratch)
    oaa = tie_field(root, GEOMETRY_MEMBER, "OAA", shape, scratch)
    slp = tie_field(root, METEO_MEMBER, "sea_level_pressure", shape, scratch)
    ozone = tie_field(root, METEO_MEMBER, "total_ozone", shape, scratch)
    vapour = tie_field(root, METEO_MEMBER, "total_columnar_water_vapour", shape, scratch)

    dataset = open_sen3_variable(
        extract_member(root, find_member(root, "geo_coordinates.nc"), scratch), "altitude"
    )
    altitude = read_packed(dataset.GetRasterBand(1), decimate=decimate, log10=False)
    dataset = None
    if altitude.shape != shape:
        altitude = altitude[: shape[0], : shape[1]]

    pressure = pixel_pressure(slp, altitude)
    azimuth = relative_azimuth(saa, oaa)
    rejected, _, bright = quality_mask(root, scratch, shape, ["invalid", "saturated"])
    land, _, _ = quality_mask(root, scratch, shape, ["land"])
    usable = ~rejected & ~bright

    surfaces = {}
    for band in BANDS:
        toa = _toa(root, band, shape, sza, scratch, decimate)
        t_gas = gas_transmittance(
            platform, band, ozone_kg_m2=ozone, water_kg_m2=vapour,
            solar_zenith_deg=sza, view_zenith_deg=oza,
        )
        terms = rayleigh_terms(
            platform, band, pressure_hpa=pressure, solar_zenith_deg=sza,
            view_zenith_deg=oza, relative_azimuth_deg=azimuth,
        )
        surfaces[band] = surface_from_toa(
            toa,
            gas_transmittance=t_gas,
            path_reflectance=terms["path_reflectance"],
            transmittance_down=terms["transmittance_down"],
            transmittance_up=terms["transmittance_up"],
            spherical_albedo=terms["spherical_albedo"],
        )
    return {
        "shape": shape,
        "scratch": scratch,
        "solar_zenith": sza,
        "land": land & usable,
        "water": ~land & usable,
        "surfaces": surfaces,
    }


def residuals(frame, reference_path, kind):
    """Median residual per band against one Level-2 reference."""
    root = sen3_root(str(reference_path))
    mask = frame["land"] if kind == "syn" else frame["water"]
    out = {}
    for band in BANDS:
        member = (
            (f"Oa{band:02d}_reflectance.nc", f"Oa{band:02d}_reflectance")
            if kind == "wfr"
            else (f"Syn_Oa{band:02d}_reflectance.nc", f"SDR_Oa{band:02d}")
        )
        try:
            l2 = level2_reflectance(root, member[0], member[1], frame["shape"], frame["scratch"])
        except (ValueError, RuntimeError):
            out[band] = (float("nan"), 0)
            continue
        surface = frame["surfaces"][band]
        keep = mask & np.isfinite(surface) & np.isfinite(l2)
        count = int(keep.sum())
        if count < MINIMUM_PIXELS:
            out[band] = (float("nan"), count)
            continue
        out[band] = (
            float(np.nanmedian(np.where(keep, surface - l2, np.nan))),
            count,
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--decimate", type=int, default=8)
    parser.add_argument("--platform", default="S3A")
    parser.add_argument("--months", nargs="+", default=["2026/10", "2026/11"])
    args = parser.parse_args()

    found = collect(args.platform, args.months)
    complete = sorted(
        key for key, kinds in found.items() if {"l1", "wfr", "syn"} <= set(kinds)
    )
    # Consecutive, not strided: the archive alternates two solar-zenith groups
    # between neighbouring acquisitions, so a stride lands on one group only -
    # a sample of three strided frames came out all at 78 to 79 degrees.
    sample = complete[args.offset:][: args.count]
    print(f"{len(complete)} complete triples; taking {len(sample)} consecutively "
          f"from offset {args.offset}\n")

    print(f"{'acquisition':18s} {'SZA':>6s} "
          f"{'land 665':>9s} {'land 560':>9s} {'land 490':>9s} "
          f"{'water 665':>10s} {'water 560':>10s} {'water 490':>10s}")
    rows = []
    for key in sample:
        paths = found[key]
        try:
            frame = corrected_frame(paths["l1"], args.decimate)
        except Exception as error:
            print(f"{key[0]:18s} unreadable: {type(error).__name__}")
            continue
        land = residuals(frame, paths["syn"], "syn")
        water = residuals(frame, paths["wfr"], "wfr")
        zenith = float(np.nanmedian(frame["solar_zenith"]))
        rows.append((zenith, land, water))
        print(f"{key[0]:18s} {zenith:6.1f} "
              + " ".join(
                  f"{land[b][0]:9.4f}" if np.isfinite(land[b][0]) else f"{'-':>9s}"
                  for b in BANDS
              )
              + " "
              + " ".join(
                  f"{water[b][0]:10.4f}" if np.isfinite(water[b][0]) else f"{'-':>10s}"
                  for b in BANDS
              ), flush=True)

    print("\nBy solar zenith band, median of the absolute land residual:")
    print(f"{'zenith':12s} {'frames':>7s} {'665':>8s} {'560':>8s} {'490':>8s} {'worst':>8s}")
    for low, high in ((0, 68), (68, 72), (72, 76), (76, 80), (80, 90)):
        inside = [(z, land) for z, land, _ in rows if low <= z < high]
        if not inside:
            continue
        per_band = []
        for band in BANDS:
            values = [abs(land[band][0]) for _, land in inside if np.isfinite(land[band][0])]
            per_band.append(float(np.median(values)) if values else float("nan"))
        worst = np.nanmax(per_band) if np.any(np.isfinite(per_band)) else float("nan")
        print(f"{low:3d}-{high:<8d} {len(inside):7d} "
              + " ".join(f"{v:8.4f}" if np.isfinite(v) else f"{'-':>8s}" for v in per_band)
              + f" {worst:8.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
